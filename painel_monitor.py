"""
Painel do Monitor Cardíaco Simulado (ESP32 + Wokwi + MQTT)
-----------------------------------------------------------
Interface gráfica para acompanhar os dados enviados pelo ESP32 e
enviar comandos para ele, usando o broker público da HiveMQ.

Requisitos:
    pip install paho-mqtt
    (o Tkinter já vem com o Python no Windows/macOS;
     no Linux: sudo apt install python3-tk)

Executar:
    python painel_monitor.py

Tópicos (precisam ser iguais ao TOPICO_BASE do sketch.ino):
    <base>/dados   -> recebe os dados a cada 2 s (JSON)
    <base>/alerta  -> recebe os alertas (JSON)
    <base>/cmd     -> envia comandos: ataque_on, ataque_off, reconhecer
"""

import csv
import json
import queue
import random
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import paho.mqtt.client as mqtt

# ----------------------------- Configuração --------------------------
BROKER_PADRAO = "broker.hivemq.com"
PORTA_PADRAO = 1883
TOPICO_BASE_PADRAO = "monitorcardiaco/paciente01"

HISTORICO_MAX = 90        # Pontos no gráfico (90 x 2 s = 3 minutos)
TIMEOUT_DADOS_S = 6       # Sem dados por mais que isso = aviso de ESP32 parado
LIMITE_BRADI = 50
LIMITE_TAQUI = 120

# Paleta azul da interface
AZUL_ESCURO = "#0d47a1"
AZUL = "#1565c0"
AZUL_MEDIO = "#1e88e5"
AZUL_CLARO = "#e3f2fd"
FUNDO = "#eef4fb"
BORDA = "#90caf9"
TEXTO = "#0d2a4d"
TEXTO_SUAVE = "#5c7a99"

ESTADOS_ALARME = {"BRADICARDIA", "TAQUICARDIA", "ARRITMIA", "ASSISTOLIA"}
CORES_STATUS = {
    "NORMAL": AZUL,
    "AGUARDANDO": "#78909c",
    "BRADICARDIA": "#ef6c00",
    "TAQUICARDIA": "#ef6c00",
    "ARRITMIA": "#c62828",
    "ASSISTOLIA": "#c62828",
}
CAMPOS_CSV = ["hora", "bpm", "bpm_ajustado", "sd_rr_ms", "cv", "amostras",
              "status", "modo", "rele", "uptime_s"]


def criar_cliente(client_id):
    """Cria o cliente MQTT funcionando tanto na paho-mqtt 2.x quanto na 1.x."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)


class PainelMonitor:
    def __init__(self, root):
        self.root = root
        self.root.title("Painel - Monitor Cardíaco (MQTT / HiveMQ)")
        self.root.geometry("1150x850")
        self.root.minsize(1000, 770)
        self.root.configure(bg=FUNDO)

        self.fila = queue.Queue()      # Mensagens vindas da thread do MQTT
        self.cliente = None
        self.conectado = False
        self.topico_base = TOPICO_BASE_PADRAO

        self.historico = deque(maxlen=HISTORICO_MAX)  # (hora, bpm, status)
        self.registros = []                           # Todos os dados (para CSV)
        self.alertas = []                             # Todos os alertas (para CSV)
        self.ultimo_dado = None

        self._montar_interface()

        self.root.after(100, self._processar_fila)
        self.root.after(1000, self._verificar_timeout)
        self.root.protocol("WM_DELETE_WINDOW", self._fechar)

    # =================================================================
    #                           Interface
    # =================================================================
    def _montar_interface(self):
        estilo = ttk.Style()
        try:
            estilo.theme_use("clam")
        except tk.TclError:
            pass
        negrito = ("TkDefaultFont", 10, "bold")
        estilo.configure(".", background=FUNDO, foreground=TEXTO)
        estilo.configure("TFrame", background=FUNDO)
        estilo.configure("TLabel", background=FUNDO, foreground=TEXTO)
        estilo.configure("TLabelframe", background=FUNDO, bordercolor=BORDA)
        estilo.configure("TLabelframe.Label", background=FUNDO, foreground=AZUL_ESCURO, font=negrito)
        estilo.configure("TEntry", fieldbackground="white", bordercolor=BORDA, lightcolor=BORDA)
        estilo.map("TEntry", fieldbackground=[("disabled", "#e1ebf5")], foreground=[("disabled", TEXTO_SUAVE)])
        estilo.configure("TButton", background=AZUL, foreground="white", bordercolor=AZUL_ESCURO,
                         lightcolor=AZUL_MEDIO, darkcolor=AZUL_ESCURO, focuscolor=AZUL_MEDIO,
                         padding=(10, 4), font=negrito)
        estilo.map("TButton",
                   background=[("pressed", AZUL_ESCURO), ("active", AZUL_MEDIO), ("disabled", "#90a4ae")],
                   foreground=[("disabled", "#eceff1")])
        estilo.configure("Cmd.TButton", padding=6)
        estilo.configure("Treeview", rowheight=24, background="white", fieldbackground="white",
                         foreground=TEXTO, bordercolor=BORDA)
        estilo.map("Treeview", background=[("selected", AZUL_MEDIO)], foreground=[("selected", "white")])
        estilo.configure("Treeview.Heading", background=AZUL, foreground="white", font=negrito,
                         relief="flat", bordercolor=AZUL_ESCURO)
        estilo.map("Treeview.Heading", background=[("active", AZUL_MEDIO)])
        estilo.configure("Vertical.TScrollbar", background=AZUL_CLARO, troughcolor=FUNDO,
                         bordercolor=BORDA, arrowcolor=AZUL)

        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=3)
        self.root.rowconfigure(4, weight=2)

        # ---------------- Cabeçalho ----------------
        cabecalho = tk.Frame(self.root, bg=AZUL_ESCURO, padx=16, pady=10)
        cabecalho.grid(row=0, column=0, sticky="ew")
        tk.Label(cabecalho, text="♥  Monitor Cardíaco", bg=AZUL_ESCURO, fg="white",
                 font=("TkDefaultFont", 17, "bold")).pack(side="left")
        tk.Label(cabecalho, text="ESP32 · Wokwi · MQTT", bg=AZUL_ESCURO, fg="#bbdefb",
                 font=("TkDefaultFont", 10)).pack(side="left", padx=14, pady=(5, 0))
        self.lbl_relogio = tk.Label(cabecalho, text="", bg=AZUL_ESCURO, fg="white",
                                    font=("TkDefaultFont", 12, "bold"))
        self.lbl_relogio.pack(side="right")

        # ---------------- Conexão ----------------
        frm_con = ttk.LabelFrame(self.root, text="Conexão MQTT", padding=8)
        frm_con.grid(row=1, column=0, sticky="ew", padx=10, pady=(10, 5))
        frm_con.columnconfigure(5, weight=1)

        self.var_broker = tk.StringVar(value=BROKER_PADRAO)
        self.var_porta = tk.StringVar(value=str(PORTA_PADRAO))
        self.var_topico = tk.StringVar(value=TOPICO_BASE_PADRAO)

        ttk.Label(frm_con, text="Broker:").grid(row=0, column=0, sticky="w")
        self.ent_broker = ttk.Entry(frm_con, textvariable=self.var_broker, width=22)
        self.ent_broker.grid(row=0, column=1, padx=(4, 12))

        ttk.Label(frm_con, text="Porta:").grid(row=0, column=2, sticky="w")
        self.ent_porta = ttk.Entry(frm_con, textvariable=self.var_porta, width=6)
        self.ent_porta.grid(row=0, column=3, padx=(4, 12))

        ttk.Label(frm_con, text="Tópico base:").grid(row=0, column=4, sticky="w")
        self.ent_topico = ttk.Entry(frm_con, textvariable=self.var_topico)
        self.ent_topico.grid(row=0, column=5, sticky="ew", padx=(4, 12))

        self.btn_conectar = ttk.Button(frm_con, text="Conectar", command=self._alternar_conexao)
        self.btn_conectar.grid(row=0, column=6, padx=(0, 12))

        self.lbl_conexao = tk.Label(frm_con, text="● Desconectado", fg="#c62828", bg=FUNDO,
                                    font=("TkDefaultFont", 10, "bold"))
        self.lbl_conexao.grid(row=0, column=7, sticky="e")

        # ---------------- Cards ----------------
        frm_cards = ttk.Frame(self.root)
        frm_cards.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        for i in range(6):
            frm_cards.columnconfigure(i, weight=1, uniform="cards")

        self.card_bpm = self._card(frm_cards, "BPM médio", 0, tamanho=28)
        self.card_sd = self._card(frm_cards, "Desvio padrão RR", 1)
        self.card_cv = self._card(frm_cards, "Coef. de variação", 2)
        self.card_status = self._card(frm_cards, "Status", 3, tamanho=16)
        self.card_modo = self._card(frm_cards, "Modo", 4, tamanho=16)
        self.card_rele = self._card(frm_cards, "Relé de alarme", 5, tamanho=16)

        # ---------------- Gráfico + comandos ----------------
        frm_meio = ttk.Frame(self.root)
        frm_meio.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        frm_meio.columnconfigure(0, weight=1)
        frm_meio.rowconfigure(0, weight=1)

        frm_graf = ttk.LabelFrame(frm_meio, text="Frequência cardíaca (últimos 3 minutos)", padding=6)
        frm_graf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.canvas = tk.Canvas(frm_graf, bg="white", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._desenhar_grafico())

        frm_lado = ttk.Frame(frm_meio)
        frm_lado.grid(row=0, column=1, sticky="ns")

        frm_cmd = ttk.LabelFrame(frm_lado, text="Comandos para o ESP32", padding=8)
        frm_cmd.pack(fill="x")
        ttk.Button(frm_cmd, text="⚡ Simular ataque cardíaco", style="Cmd.TButton",
                   command=lambda: self._enviar_comando("ataque_on")).pack(fill="x", pady=3)
        ttk.Button(frm_cmd, text="♥ Voltar ao ritmo normal", style="Cmd.TButton",
                   command=lambda: self._enviar_comando("ataque_off")).pack(fill="x", pady=3)
        ttk.Button(frm_cmd, text="✔ Reconhecer alarme", style="Cmd.TButton",
                   command=lambda: self._enviar_comando("reconhecer")).pack(fill="x", pady=3)

        frm_info = ttk.LabelFrame(frm_lado, text="Detalhes", padding=8)
        frm_info.pack(fill="x", pady=(8, 0))
        self.lbl_ajustado = self._linha_info(frm_info, "BPM no potenciômetro:")
        self.lbl_amostras = self._linha_info(frm_info, "Amostras na janela:")
        self.lbl_uptime = self._linha_info(frm_info, "Tempo ligado (ESP32):")
        self.lbl_atualizacao = self._linha_info(frm_info, "Último dado:")
        self.lbl_qtd = self._linha_info(frm_info, "Mensagens recebidas:")

        # ---------------- Alertas ----------------
        frm_alertas = ttk.LabelFrame(self.root, text="Alertas e eventos", padding=6)
        frm_alertas.grid(row=4, column=0, sticky="nsew", padx=10, pady=(5, 10))
        frm_alertas.columnconfigure(0, weight=1)
        frm_alertas.rowconfigure(0, weight=1)

        colunas = ("hora", "tipo", "severidade", "detalhes")
        self.tree = ttk.Treeview(frm_alertas, columns=colunas, show="headings", height=5)
        for col, titulo, largura, estica in (("hora", "Hora", 80, False),
                                              ("tipo", "Tipo", 120, False),
                                              ("severidade", "Severidade", 90, False),
                                              ("detalhes", "Detalhes", 600, True)):
            self.tree.heading(col, text=titulo)
            self.tree.column(col, width=largura, stretch=estica, anchor="w")
        self.tree.tag_configure("critico", background="#ffcdd2")
        self.tree.tag_configure("atencao", background="#ffe0b2")
        self.tree.tag_configure("info", background=AZUL_CLARO)
        self.tree.tag_configure("comando", background="#d0e4fa")
        self.tree.tag_configure("aviso", background="#f4f7fb")
        self.tree.grid(row=0, column=0, sticky="nsew")

        barra = ttk.Scrollbar(frm_alertas, orient="vertical", command=self.tree.yview)
        barra.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=barra.set)

        frm_btns = ttk.Frame(frm_alertas)
        frm_btns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(6, 0))
        ttk.Button(frm_btns, text="Limpar lista", command=self._limpar_alertas).pack(side="left", padx=4)
        ttk.Button(frm_btns, text="Exportar CSV", command=self._exportar_csv).pack(side="left", padx=4)

        self.qtd_mensagens = 0
        self._desenhar_grafico()

    def _card(self, pai, titulo, coluna, tamanho=20):
        frame = tk.Frame(pai, bg="white", highlightbackground=BORDA,
                         highlightthickness=2, padx=12, pady=8)
        frame.grid(row=0, column=coluna, sticky="nsew", padx=4)
        lbl_titulo = tk.Label(frame, text=titulo, bg="white", fg=AZUL,
                              font=("TkDefaultFont", 9, "bold"))
        lbl_titulo.pack(anchor="w")
        lbl_valor = tk.Label(frame, text="--", bg="white", fg=TEXTO,
                             font=("TkDefaultFont", tamanho, "bold"))
        lbl_valor.pack(anchor="w")
        return frame, lbl_titulo, lbl_valor

    def _pintar_card(self, card, texto, cor_fundo="white", cor_texto=TEXTO):
        frame, lbl_titulo, lbl_valor = card
        branco = cor_fundo == "white"
        frame.config(bg=cor_fundo, highlightbackground=BORDA if branco else cor_fundo)
        lbl_titulo.config(bg=cor_fundo, fg=AZUL if branco else "white")
        lbl_valor.config(text=texto, bg=cor_fundo, fg=cor_texto)

    def _linha_info(self, pai, texto):
        linha = ttk.Frame(pai)
        linha.pack(fill="x", pady=1)
        ttk.Label(linha, text=texto).pack(side="left")
        valor = ttk.Label(linha, text="--", font=("TkDefaultFont", 9, "bold"))
        valor.pack(side="right")
        return valor

    # =================================================================
    #                              MQTT
    # =================================================================
    def _alternar_conexao(self):
        if self.cliente:
            self._desconectar()
        else:
            self._conectar()

    def _conectar(self):
        host = self.var_broker.get().strip()
        base = self.var_topico.get().strip().rstrip("/")
        try:
            porta = int(self.var_porta.get())
        except ValueError:
            messagebox.showerror("Porta inválida", "A porta precisa ser um número (ex.: 1883).")
            return
        if not host or not base:
            messagebox.showerror("Dados incompletos", "Preencha o broker e o tópico base.")
            return

        self.topico_base = base
        self.cliente = criar_cliente(f"painel-python-{random.randint(0, 999999)}")
        self.cliente.on_connect = self._on_connect
        self.cliente.on_disconnect = self._on_disconnect
        self.cliente.on_message = self._on_message
        self.cliente.reconnect_delay_set(min_delay=1, max_delay=10)

        try:
            self.cliente.connect_async(host, porta, keepalive=30)
            self.cliente.loop_start()   # Thread própria: a interface não trava
        except Exception as erro:
            self.cliente = None
            messagebox.showerror("Erro de conexão", str(erro))
            return

        self.btn_conectar.config(text="Desconectar")
        for entrada in (self.ent_broker, self.ent_porta, self.ent_topico):
            entrada.config(state="disabled")
        self._status_conexao("● Conectando...", "#f9a825")

    def _desconectar(self):
        if self.cliente:
            cliente, self.cliente = self.cliente, None
            try:
                cliente.disconnect()
                cliente.loop_stop()
            except Exception:
                pass
        self.conectado = False
        self.btn_conectar.config(text="Conectar")
        for entrada in (self.ent_broker, self.ent_porta, self.ent_topico):
            entrada.config(state="normal")
        self._status_conexao("● Desconectado", "#c62828")

    # Os callbacks abaixo rodam na thread do MQTT: só colocam itens na fila.
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        falhou = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        if falhou:
            self.fila.put(("erro", f"Broker recusou a conexão ({rc})"))
            return
        client.subscribe(self.topico_base + "/#")
        self.fila.put(("conectado", None))

    def _on_disconnect(self, client, userdata, *args):
        self.fila.put(("desconectado", None))

    def _on_message(self, client, userdata, msg):
        self.fila.put(("mensagem", (msg.topic, msg.payload.decode("utf-8", errors="replace"))))

    def _enviar_comando(self, comando):
        if not self.cliente or not self.conectado:
            messagebox.showwarning("Sem conexão", "Conecte-se ao broker antes de enviar comandos.")
            return
        self.cliente.publish(self.topico_base + "/cmd", comando)

    # =================================================================
    #                 Processamento (thread da interface)
    # =================================================================
    def _processar_fila(self):
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "conectado":
                    self.conectado = True
                    self._status_conexao("● Conectado", AZUL)
                    self._log("CONEXÃO", "info", f"Conectado. Assinando {self.topico_base}/#")
                elif tipo == "desconectado":
                    self.conectado = False
                    if self.cliente:   # Caiu sozinho: o paho tenta reconectar
                        self._status_conexao("● Reconectando...", "#f9a825")
                elif tipo == "erro":
                    self._log("CONEXÃO", "aviso", dado)
                elif tipo == "mensagem":
                    self._tratar_mensagem(*dado)
        except queue.Empty:
            pass
        self.root.after(100, self._processar_fila)

    def _tratar_mensagem(self, topico, payload):
        self.qtd_mensagens += 1
        self.lbl_qtd.config(text=str(self.qtd_mensagens))

        if topico.endswith("/dados"):
            self._tratar_dados(payload)
        elif topico.endswith("/alerta"):
            self._tratar_alerta(payload)
        elif topico.endswith("/cmd"):
            self._log("COMANDO", "comando", f"Comando '{payload}' entregue ao broker")

    def _ler_json(self, payload):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            self._log("ERRO", "aviso", f"Mensagem não é um JSON válido: {payload[:100]}")
            return None

    def _tratar_dados(self, payload):
        d = self._ler_json(payload)
        if d is None:
            return

        agora = datetime.now()
        self.ultimo_dado = time.time()

        bpm = float(d.get("bpm", 0))
        sd = float(d.get("sd_rr_ms", 0))
        cv = float(d.get("cv", 0))
        status = str(d.get("status", "--"))
        modo = str(d.get("modo", "--"))
        rele = int(d.get("rele", 0))

        cor_status = CORES_STATUS.get(status, "#757575")
        self._pintar_card(self.card_bpm, f"{bpm:.0f}",
                          cor_texto=cor_status if status in ESTADOS_ALARME else AZUL_ESCURO)
        self._pintar_card(self.card_sd, f"{sd:.1f} ms")
        self._pintar_card(self.card_cv, f"{cv:.3f}",
                          cor_texto="#c62828" if cv > 0.15 else TEXTO)
        self._pintar_card(self.card_status, status, cor_fundo=cor_status, cor_texto="white")
        if modo == "ataque":
            self._pintar_card(self.card_modo, "ATAQUE", cor_fundo="#c62828", cor_texto="white")
        else:
            self._pintar_card(self.card_modo, "Normal")
        if rele:
            self._pintar_card(self.card_rele, "LIGADO", cor_fundo="#c62828", cor_texto="white")
        else:
            self._pintar_card(self.card_rele, "Desligado")

        self.lbl_ajustado.config(text=str(d.get("bpm_ajustado", "--")))
        self.lbl_amostras.config(text=f"{d.get('amostras', '--')} / 10")
        uptime = int(d.get("uptime_s", 0))
        self.lbl_uptime.config(text=f"{uptime // 60} min {uptime % 60:02d} s")

        self.historico.append((agora, bpm, status))
        self.registros.append({"hora": agora.isoformat(sep=" ", timespec="seconds"), **d})
        if len(self.registros) > 50000:
            del self.registros[:10000]
        self._desenhar_grafico()

    def _tratar_alerta(self, payload):
        d = self._ler_json(payload)
        if d is None:
            return

        if "evento" in d:
            self._log("RECONHECIDO", "info",
                      f"Alarme {d.get('alerta', '?')} reconhecido via {d.get('origem', '?')}")
            return

        tipo = d.get("alerta", "?")
        severidade = d.get("severidade", "info")
        bpm = d.get("bpm", "--")
        sd = d.get("sd_rr_ms", "--")

        if tipo == "NORMALIZADO":
            detalhes = f"Batimento voltou ao normal | BPM {bpm} | SD {sd} ms"
        else:
            detalhes = f"{d.get('mensagem', '')} | BPM {bpm} | SD {sd} ms | CV {d.get('cv', '--')}"

        self._log(tipo, severidade, detalhes)
        if severidade == "critico":
            self.root.bell()

    def _log(self, tipo, severidade, detalhes):
        hora = datetime.now().strftime("%H:%M:%S")
        self.tree.insert("", 0, values=(hora, tipo, severidade, detalhes), tags=(severidade,))
        self.alertas.append({"hora": hora, "tipo": tipo, "severidade": severidade, "detalhes": detalhes})

    def _verificar_timeout(self):
        self.lbl_relogio.config(text=datetime.now().strftime("%d/%m/%Y  %H:%M:%S"))
        if self.ultimo_dado is not None:
            atraso = int(time.time() - self.ultimo_dado)
            if self.conectado and atraso > TIMEOUT_DADOS_S:
                self.lbl_atualizacao.config(text=f"há {atraso} s (ESP32 parado?)", foreground="#c62828")
            else:
                self.lbl_atualizacao.config(text=f"há {atraso} s", foreground="")
        self.root.after(1000, self._verificar_timeout)

    def _status_conexao(self, texto, cor):
        self.lbl_conexao.config(text=texto, fg=cor)

    # =================================================================
    #                              Gráfico
    # =================================================================
    def _desenhar_grafico(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return

        esq, dir_, topo, base = 42, 12, 12, 24
        y_min, y_max = 0, 220
        fonte = ("TkDefaultFont", 8)

        def y(valor):
            valor = min(max(valor, y_min), y_max)
            return topo + (h - topo - base) * (1 - (valor - y_min) / (y_max - y_min))

        def x(indice):
            return esq + (w - esq - dir_) * indice / (HISTORICO_MAX - 1)

        # Grade e eixo Y
        for v in range(0, y_max + 1, 40):
            c.create_line(esq, y(v), w - dir_, y(v), fill="#e3ecf7")
            c.create_text(esq - 6, y(v), text=str(v), anchor="e", fill=TEXTO_SUAVE, font=fonte)

        # Faixa normal e limites
        c.create_rectangle(esq, y(LIMITE_TAQUI), w - dir_, y(LIMITE_BRADI),
                           fill="#e8f1fc", outline="")
        for limite, nome, desloc in ((LIMITE_TAQUI, "taquicardia (120)", -9),
                                     (LIMITE_BRADI, "bradicardia (50)", -9)):
            c.create_line(esq, y(limite), w - dir_, y(limite), fill="#ef6c00", dash=(5, 3))
            c.create_text(w - dir_ - 4, y(limite) + desloc, text=nome, anchor="e",
                          fill="#ef6c00", font=fonte)

        pontos = list(self.historico)
        if not pontos:
            c.create_text(w / 2, h / 2, text="Aguardando dados do ESP32...",
                          fill=TEXTO_SUAVE, font=("TkDefaultFont", 11))
            return

        deslocamento = HISTORICO_MAX - len(pontos)
        if len(pontos) > 1:
            coords = []
            for i, (_, bpm, _) in enumerate(pontos):
                coords += [x(i + deslocamento), y(bpm)]
            c.create_line(*coords, fill=AZUL_ESCURO, width=2)

        for i, (_, bpm, status) in enumerate(pontos):
            if status in ESTADOS_ALARME:
                cx, cy = x(i + deslocamento), y(bpm)
                c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3,
                              fill=CORES_STATUS[status], outline="")

        # Valor atual e horários
        _, bpm_atual, status_atual = pontos[-1]
        c.create_text(esq + 6, topo + 4, anchor="nw", fill=CORES_STATUS.get(status_atual, TEXTO),
                      text=f"{bpm_atual:.0f} BPM - {status_atual}", font=("TkDefaultFont", 10, "bold"))
        c.create_text(x(deslocamento), h - base + 12, text=pontos[0][0].strftime("%H:%M:%S"),
                      anchor="w", fill=TEXTO_SUAVE, font=fonte)
        c.create_text(x(HISTORICO_MAX - 1), h - base + 12, text=pontos[-1][0].strftime("%H:%M:%S"),
                      anchor="e", fill=TEXTO_SUAVE, font=fonte)

    # =================================================================
    #                          Ações extras
    # =================================================================
    def _limpar_alertas(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

    def _exportar_csv(self):
        if not self.registros and not self.alertas:
            messagebox.showinfo("Exportar", "Ainda não há dados para exportar.")
            return
        caminho = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"monitor_{datetime.now():%Y%m%d_%H%M%S}.csv",
            filetypes=[("CSV", "*.csv")])
        if not caminho:
            return

        with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
            escritor = csv.DictWriter(f, fieldnames=CAMPOS_CSV, extrasaction="ignore", delimiter=";")
            escritor.writeheader()
            escritor.writerows(self.registros)

        caminho_alertas = caminho[:-4] + "_alertas.csv" if caminho.lower().endswith(".csv") \
            else caminho + "_alertas.csv"
        with open(caminho_alertas, "w", newline="", encoding="utf-8-sig") as f:
            escritor = csv.DictWriter(f, fieldnames=["hora", "tipo", "severidade", "detalhes"], delimiter=";")
            escritor.writeheader()
            escritor.writerows(self.alertas)

        messagebox.showinfo("Exportar", f"Arquivos salvos:\n{caminho}\n{caminho_alertas}")

    def _fechar(self):
        self._desconectar()
        self.root.destroy()


if __name__ == "__main__":
    janela = tk.Tk()
    PainelMonitor(janela)
    janela.mainloop()
