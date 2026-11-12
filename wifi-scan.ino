// =====================================================================
//  Simulador de Monitor Cardíaco - ESP32 + MQTT (Wokwi)
// ---------------------------------------------------------------------
//  - Potenciômetro (GPIO34): ajusta a frequência cardíaca (30 a 200 BPM)
//  - Chave deslizante (GPIO27): ativa o modo "ataque cardíaco"
//    (ritmo caótico/fibrilação com pausas = assistolia)
//  - LED vermelho (GPIO2): pisca a cada batimento
//  - Relé (GPIO26): é ACIONADO pelo ESP32 quando há alarme
//    (simula ligar uma sirene / chamar a equipe médica) -> LED de alarme
//  - Botão verde (GPIO14): reconhece/silencia o alarme (desliga o relé)
//  - LCD 16x2 I2C (SDA 21 / SCL 22): mostra BPM, desvio padrão e status
//
//  Análise: guarda os últimos 10 intervalos entre batimentos (intervalos RR)
//  e calcula média, desvio padrão (SD) e coeficiente de variação (CV = SD/média).
//    ASSISTOLIA  -> nenhum batimento há mais de 3 s
//    ARRITMIA    -> CV > 0,15 (desvio padrão incomum = ritmo irregular)
//    TAQUICARDIA -> BPM médio > 120
//    BRADICARDIA -> BPM médio < 50
//
//  Tópicos MQTT (troque TOPICO_BASE por algo único seu!):
//    <base>/dados  -> publica a cada 2 s os dados do batimento (JSON)
//    <base>/alerta -> publica os alertas (JSON)
//    <base>/cmd    -> recebe comandos: "ataque_on", "ataque_off", "reconhecer"
// =====================================================================

#include <WiFi.h>              // Comunicação Wi-Fi
#include <PubSubClient.h>      // Comunicação MQTT
#include <Wire.h>              // Barramento I2C
#include <LiquidCrystal_I2C.h> // Display LCD I2C

// ----------------------------- Rede / MQTT ---------------------------
#define SSID        "Wokwi-GUEST"
#define PASS        ""
#define MQTTSERVER  "broker.hivemq.com"  // Mesmo broker usado pelo painel Python
#define PORT        1883

#define TOPICO_BASE   "monitorcardiaco/paciente01"   // <-- personalize para não colidir com outros usuários
#define TOPICO_DADOS  TOPICO_BASE "/dados"
#define TOPICO_ALERTA TOPICO_BASE "/alerta"
#define TOPICO_CMD    TOPICO_BASE "/cmd"

// ------------------------------- Pinos -------------------------------
#define PIN_POT       34   // Potenciômetro (velocidade do batimento)
#define PIN_ATAQUE    27   // Chave deslizante (LOW = ataque cardíaco)
#define PIN_BOTAO     14   // Botão de reconhecer alarme
#define PIN_LED_BAT    2   // LED que pisca a cada batimento
#define PIN_RELE      26   // Relé de alarme

#define RELE_LIGADO   HIGH
#define RELE_DESLIGADO LOW

// --------------------------- Parâmetros clínicos ---------------------
#define BPM_MIN               30
#define BPM_MAX               200
#define BPM_CENTRO            75      // BPM com o potenciômetro no meio
#define JANELA                10      // Quantos intervalos RR entram no cálculo
#define MIN_AMOSTRAS          5       // Mínimo de intervalos para classificar
#define LIMITE_TAQUI          120     // BPM
#define LIMITE_BRADI          50      // BPM
#define LIMITE_CV             0.15    // Desvio padrão / média dos intervalos RR
#define LIMITE_ASSISTOLIA_MS  3000    // Sem batimento por mais que isso = assistolia

// ------------------------------ Tempos -------------------------------
#define INTERVALO_ENVIO_MS    1000    // Envio de dados ao servidor
#define REPETIR_ALERTA_MS     10000   // Repete o alerta enquanto não for reconhecido
#define INTERVALO_ANALISE_MS  250
#define INTERVALO_LCD_MS      300
#define PULSO_LED_MS          80

// ---------------------------------------------------------------------
enum Estado { AGUARDANDO, NORMAL, BRADICARDIA, TAQUICARDIA, ARRITMIA, ASSISTOLIA };
const char* NOMES_ESTADO[] = { "AGUARDANDO", "NORMAL", "BRADICARDIA", "TAQUICARDIA", "ARRITMIA", "ASSISTOLIA" };

WiFiClient espClient;
PubSubClient client(espClient);
LiquidCrystal_I2C lcd(0x27, 16, 2);
char clientId[50];

// Janela circular com os intervalos RR (ms)
unsigned long rr[JANELA];
int rrQtd = 0;
int rrPos = 0;

unsigned long ultimoBatimento  = 0;
unsigned long proximoBatimento = 0;
unsigned long fimPulsoLed      = 0;

int  bpmAlvo      = 75;   // Valor lido do potenciômetro
int  bpmAgendado  = 75;   // BPM usado para agendar o batimento atual (-1 = gerado no modo ataque)
int  bpmDaJanela  = 75;   // BPM ao qual os intervalos da janela pertencem
bool modoAtaque   = false;
bool ataqueRemoto = false; // Ataque ativado via MQTT

float bpmMedio = 0;
float sdRR     = 0;
float cvRR     = 0;

Estado estado         = AGUARDANDO;
Estado estadoAnterior = AGUARDANDO;
bool   alarmeReconhecido = false;

unsigned long ultimoAlerta  = 0;
unsigned long ultimoEnvio   = 0;
unsigned long ultimaAnalise = 0;
unsigned long ultimoLcd     = 0;

// =====================================================================
//                      Janela de intervalos RR
// =====================================================================
void limparJanela()
{
  rrQtd = 0;
  rrPos = 0;
}

void adicionarRR(unsigned long intervalo)
{
  rr[rrPos] = intervalo;
  rrPos = (rrPos + 1) % JANELA;
  if (rrQtd < JANELA) rrQtd++;
}

void calcularEstatisticas()
{
  if (rrQtd < 2) return;

  float soma = 0;
  for (int i = 0; i < rrQtd; i++) soma += rr[i];
  float media = soma / rrQtd;

  float somaQuad = 0;
  for (int i = 0; i < rrQtd; i++)
  {
    float d = rr[i] - media;
    somaQuad += d * d;
  }

  sdRR     = sqrt(somaQuad / (rrQtd - 1)); // Desvio padrão amostral
  cvRR     = sdRR / media;
  bpmMedio = 60000.0 / media;
}

int lerBpmAlvo()
{
  static float filtro = -1;
  int bruto = analogRead(PIN_POT);
  if (filtro < 0) filtro = bruto;
  filtro = filtro * 0.9 + bruto * 0.1;           // Suaviza o ruído do ADC

  // Mapeamento em duas faixas: o MEIO do potenciômetro = 75 BPM (ritmo normal)
  //   esquerda (0%)  -> 30 BPM  | meio (50%) -> 75 BPM | direita (100%) -> 200 BPM
  //   faixa normal (50 a 120 BPM) fica entre ~22% e ~68% do curso
  int f = (int)filtro;
  if (f <= 2048) return map(f, 0, 2048, BPM_MIN, BPM_CENTRO);
  return map(f, 2048, 4095, BPM_CENTRO, BPM_MAX);
}

void agendarProximoBatimento(unsigned long agora)
{
  unsigned long intervalo;

  if (modoAtaque)
  {
    // Ataque cardíaco: ritmo caótico (fibrilação) com pausas ocasionais (assistolia)
    if (random(100) < 8) intervalo = random(3200, 4500);
    else                 intervalo = random(220, 1300);
    bpmAgendado = -1;
  }
  else
  {
    // Ritmo normal: intervalo = 60000 / BPM com variação natural de ±3%
    float base = 60000.0 / bpmAlvo;
    float variacao = random(-30, 31) / 1000.0;
    intervalo = (unsigned long)(base * (1.0 + variacao));
    bpmAgendado = bpmAlvo;
  }

  proximoBatimento = agora + intervalo;
}

void batimento(unsigned long agora)
{
  digitalWrite(PIN_LED_BAT, HIGH);
  fimPulsoLed = agora + PULSO_LED_MS;

  if (ultimoBatimento > 0)
  {
    unsigned long intervalo = agora - ultimoBatimento;

    if (bpmAgendado < 0)
    {
      // Intervalo gerado no modo ataque: só entra se ainda estivermos nele
      if (modoAtaque) adicionarRR(intervalo);
    }
    else
    {
      // Se o potenciômetro mudou bastante, recomeça a janela
      // (evita falso alarme de "desvio padrão alto" ao girar o botão)
      if (abs(bpmAgendado - bpmDaJanela) > 3)
      {
        limparJanela();
        bpmDaJanela = bpmAgendado;
      }
      adicionarRR(intervalo);
    }
  }

  ultimoBatimento = agora;
  agendarProximoBatimento(agora);
}

void atualizarModo(unsigned long agora)
{
  bool chaveAtaque = (digitalRead(PIN_ATAQUE) == LOW);
  bool novoModo = chaveAtaque || ataqueRemoto;

  if (novoModo == modoAtaque) return;
  modoAtaque = novoModo;

  Serial.print("Modo alterado para: ");
  Serial.println(modoAtaque ? "ATAQUE CARDIACO" : "NORMAL");

  if (!modoAtaque)
  {
    // Saiu do ataque: limpa a janela e volta logo ao ritmo normal
    limparJanela();
    bpmDaJanela = bpmAlvo;
    unsigned long proximoNormal = agora + 60000UL / bpmAlvo;
    if (proximoBatimento > proximoNormal) proximoBatimento = proximoNormal;
  }
}


bool ehAlarme(Estado e)
{
  return e == BRADICARDIA || e == TAQUICARDIA || e == ARRITMIA || e == ASSISTOLIA;
}

void classificar(unsigned long agora)
{
  if (ultimoBatimento > 0 && agora - ultimoBatimento > LIMITE_ASSISTOLIA_MS)
  {
    estado = ASSISTOLIA;
    return;
  }

  // Poucas amostras (ex.: logo após girar o potenciômetro): mantém o estado atual
  if (rrQtd < MIN_AMOSTRAS) return;

  calcularEstatisticas();

  if      (cvRR > LIMITE_CV)          estado = ARRITMIA;
  else if (bpmMedio > LIMITE_TAQUI)   estado = TAQUICARDIA;
  else if (bpmMedio < LIMITE_BRADI)   estado = BRADICARDIA;
  else                                estado = NORMAL;
}

const char* severidade(Estado e)
{
  return (e == ARRITMIA || e == ASSISTOLIA) ? "critico" : "atencao";
}

const char* mensagemAlerta(Estado e)
{
  switch (e)
  {
    case TAQUICARDIA: return "Frequencia cardiaca acima de 120 BPM";
    case BRADICARDIA: return "Frequencia cardiaca abaixo de 50 BPM";
    case ARRITMIA:    return "Desvio padrao dos intervalos RR muito alto (ritmo irregular / possivel ataque cardiaco)";
    case ASSISTOLIA:  return "Nenhum batimento detectado ha mais de 3 segundos";
    default:          return "";
  }
}

void publicar(const char* topico, const char* payload)
{
  Serial.print("[");
  Serial.print(topico);
  Serial.print("] ");
  Serial.println(payload);

  if (client.connected()) client.publish(topico, payload);
  else Serial.println("  (MQTT desconectado - mensagem nao enviada)");
}

float bpmReportado()
{
  return (estado == ASSISTOLIA) ? 0 : bpmMedio;
}

void publicarAlerta()
{
  char json[300];
  snprintf(json, sizeof(json),
           "{\"alerta\":\"%s\",\"severidade\":\"%s\",\"bpm\":%.0f,\"sd_rr_ms\":%.1f,\"cv\":%.3f,\"mensagem\":\"%s\"}",
           NOMES_ESTADO[estado], severidade(estado), bpmReportado(), sdRR, cvRR, mensagemAlerta(estado));
  publicar(TOPICO_ALERTA, json);
}

void publicarNormalizado()
{
  char json[150];
  snprintf(json, sizeof(json),
           "{\"alerta\":\"NORMALIZADO\",\"severidade\":\"info\",\"bpm\":%.0f,\"sd_rr_ms\":%.1f}",
           bpmMedio, sdRR);
  publicar(TOPICO_ALERTA, json);
}

void tratarAlertas(unsigned long agora)
{
  bool emAlarme = ehAlarme(estado);

  if (emAlarme)
  {
    bool novoAlarme = !ehAlarme(estadoAnterior);
    bool mudouTipo  = (estado != estadoAnterior);

    if (novoAlarme) alarmeReconhecido = false; // Alarme novo: rearma o relé

    if (mudouTipo || (!alarmeReconhecido && agora - ultimoAlerta >= REPETIR_ALERTA_MS))
    {
      publicarAlerta();
      ultimoAlerta = agora;
    }
  }
  else if (ehAlarme(estadoAnterior) && estado == NORMAL)
  {
    publicarNormalizado();
    alarmeReconhecido = false;
  }

  digitalWrite(PIN_RELE, (emAlarme && !alarmeReconhecido) ? RELE_LIGADO : RELE_DESLIGADO);
  estadoAnterior = estado;
}

void reconhecerAlarme(const char* origem)
{
  if (!ehAlarme(estado) || alarmeReconhecido) return;

  alarmeReconhecido = true;
  digitalWrite(PIN_RELE, RELE_DESLIGADO);

  char json[120];
  snprintf(json, sizeof(json),
           "{\"evento\":\"alarme_reconhecido\",\"origem\":\"%s\",\"alerta\":\"%s\"}",
           origem, NOMES_ESTADO[estado]);
  publicar(TOPICO_ALERTA, json);
}

void publicarDados()
{
  char json[300];
  snprintf(json, sizeof(json),
           "{\"bpm\":%.0f,\"bpm_ajustado\":%d,\"sd_rr_ms\":%.1f,\"cv\":%.3f,\"amostras\":%d,"
           "\"status\":\"%s\",\"modo\":\"%s\",\"rele\":%d,\"uptime_s\":%lu}",
           bpmReportado(), bpmAlvo, sdRR, cvRR, rrQtd,
           NOMES_ESTADO[estado], modoAtaque ? "ataque" : "normal",
           digitalRead(PIN_RELE) == RELE_LIGADO ? 1 : 0, millis() / 1000);
  publicar(TOPICO_DADOS, json);
}

// =====================================================================
//                         Botão e display
// =====================================================================
void lerBotao(unsigned long agora)
{
  static bool ultimoNivel = HIGH;
  static unsigned long ultimaMudanca = 0;

  bool nivel = digitalRead(PIN_BOTAO);
  if (nivel != ultimoNivel && agora - ultimaMudanca > 50) // Debounce
  {
    ultimaMudanca = agora;
    ultimoNivel = nivel;
    if (nivel == LOW) reconhecerAlarme("botao");
  }
}

void atualizarLcd()
{
  char l1[17];
  char l2[17];

  if (estado == ASSISTOLIA)
    snprintf(l1, sizeof(l1), "BPM:  0 SD:---ms");
  else if (estado == AGUARDANDO || rrQtd < MIN_AMOSTRAS)
    snprintf(l1, sizeof(l1), "BPM:--- SD:---ms");
  else
    snprintf(l1, sizeof(l1), "BPM:%3d SD:%3dms",
             constrain((int)(bpmMedio + 0.5), 0, 999),
             constrain((int)(sdRR + 0.5), 0, 999));

  // Indicadores: A = modo ataque, R = relé ligado, M = MQTT conectado
  snprintf(l2, sizeof(l2), "%-11s %c%c%c ",
           NOMES_ESTADO[estado],
           modoAtaque ? 'A' : ' ',
           digitalRead(PIN_RELE) == RELE_LIGADO ? 'R' : ' ',
           client.connected() ? 'M' : ' ');

  lcd.setCursor(0, 0);
  lcd.print(l1);
  lcd.setCursor(0, 1);
  lcd.print(l2);
}

// =====================================================================
//                                MQTT
// =====================================================================
void callback(char* topic, byte* message, unsigned int length)
{
  String stMessage;
  for (unsigned int i = 0; i < length; i++) stMessage += (char)message[i];
  stMessage.trim();

  Serial.print("Mensagem recebida no topico: ");
  Serial.print(topic);
  Serial.print(". Mensagem: ");
  Serial.println(stMessage);

  if (String(topic) == TOPICO_CMD)
  {
    if      (stMessage == "ataque_on")  ataqueRemoto = true;
    else if (stMessage == "ataque_off") ataqueRemoto = false;
    else if (stMessage == "reconhecer") reconhecerAlarme("mqtt");
  }
}

bool conectarMQTT()
{
  Serial.print("Tentando conexao MQTT... ");
  sprintf(clientId, "monitor-%lu", (unsigned long)random(1000000));

  if (client.connect(clientId))
  {
    Serial.print(clientId);
    Serial.println(" conectado");
    client.subscribe(TOPICO_CMD);
    return true;
  }

  Serial.print("falhou, rc=");
  Serial.println(client.state());
  return false;
}

// Não bloqueante: tenta reconectar a cada 5 s sem travar a simulação
void mqttReconnect()
{
  static unsigned long ultimaTentativa = 0;
  if (millis() - ultimaTentativa < 5000) return;
  ultimaTentativa = millis();
  conectarMQTT();
}

// =====================================================================
//                            Setup / Loop
// =====================================================================
void setup()
{
  Serial.begin(115200);

  pinMode(PIN_LED_BAT, OUTPUT);
  pinMode(PIN_RELE, OUTPUT);
  pinMode(PIN_ATAQUE, INPUT);         // A chave liga o pino em 3V3 ou GND
  pinMode(PIN_BOTAO, INPUT_PULLUP);
  digitalWrite(PIN_RELE, RELE_DESLIGADO);

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print("Monitor Cardiaco");
  lcd.setCursor(0, 1);
  lcd.print("Conectando WiFi");

  Serial.print("Se conectando a: ");
  Serial.println(SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(SSID, PASS);
  while (WiFi.status() != WL_CONNECTED)
  {
    delay(500);
    Serial.print(".");
  }

  Serial.println("");
  Serial.println("Conexao Wi-Fi realizada");
  Serial.print("Endereco IP: ");
  Serial.println(WiFi.localIP());

  randomSeed(micros());

  client.setServer(MQTTSERVER, PORT);
  client.setCallback(callback);
  client.setBufferSize(512);

  lcd.setCursor(0, 1);
  lcd.print("Conectando MQTT ");
  for (int i = 0; i < 3 && !conectarMQTT(); i++) delay(2000);

  Serial.println("Topicos:");
  Serial.println("  dados : " TOPICO_DADOS);
  Serial.println("  alerta: " TOPICO_ALERTA);
  Serial.println("  cmd   : " TOPICO_CMD);

  lcd.clear();
  bpmAlvo = lerBpmAlvo();
  bpmDaJanela = bpmAlvo;
  agendarProximoBatimento(millis());
}

void loop()
{
  unsigned long agora = millis();

  if (!client.connected()) mqttReconnect();
  else client.loop();

  bpmAlvo = lerBpmAlvo();
  atualizarModo(agora);
  lerBotao(agora);

  // Gera o batimento simulado
  if ((long)(agora - proximoBatimento) >= 0) batimento(agora);

  // Apaga o LED após o pulso
  if (fimPulsoLed != 0 && (long)(agora - fimPulsoLed) >= 0)
  {
    digitalWrite(PIN_LED_BAT, LOW);
    fimPulsoLed = 0;
  }

  if (agora - ultimaAnalise >= INTERVALO_ANALISE_MS)
  {
    ultimaAnalise = agora;
    classificar(agora);
    tratarAlertas(agora);
  }

  if (agora - ultimoEnvio >= INTERVALO_ENVIO_MS)
  {
    ultimoEnvio = agora;
    publicarDados();
  }

  if (agora - ultimoLcd >= INTERVALO_LCD_MS)
  {
    ultimoLcd = agora;
    atualizarLcd();
  }
}
