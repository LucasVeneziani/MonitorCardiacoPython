Projeto no Wokwi: https://wokwi.com/projects/479872168818208769

precisa instalar dependencias do python para funcionar o painel

dependencias principais:

tkinter
paho.mtt.client

Monitor Cardíaco IoT: ESP32, MQTT com Python

O projeto simula batimentos cardíacos em um ESP32 no Wokwi e envia a telemetria por MQTT, que guarda os dados e mostra os gráficos.

´´´
1. Arquitetura
´´´
   
Os dados vão em um sentido só: do ESP32 ao Painel no Python, que guarda o histórico e mostra os gráficos.
1.	ESP32 (Wokwi): gera batimentos simulados, calcula média, desvio padrão e coeficiente de variação dos intervalos RR, classifica o ritmo e aciona LED, relé e LCD.
2.	Broker MQTT do Hive: broker.hivemq.com, porta 1883. Recebe as publicações em channels/<channelID>/publish e grava cada valor no campo correspondente do canal.
3.	Painel do Python: guarda o histórico e desenha um gráfico por campo, que acompanha pelo navegador.

´´´
2. Materiais fornecidos
´´´
   
Material	Link	Papel no projeto
Exemplo 20 do professor (ESP32 + MQTT + ADC)	GitHub
Referência de cliente MQTT em Python (assina um tópico e plota com Matplotlib); não usado na versão com ThingSpeak
MQTT.Cool Test Client	testclient-cloud.mqtt.cool
Cliente web de teste da versão anterior (broker público)
Painel no Python	PainelPython
Cliente e Painel com gráficos para a visualização e execução de testes simulados
Projeto "Cardiaco" no Wokwi (autor: lucasveneziani)	Wokwi
Simulação do ESP32 com potenciômetro, chave, botão, LED, relé e LCD 16x2 I2C


´´´
3. Simulação no Wokwi
´´´
   
O sketch simula um monitor cardíaco: o potenciômetro define a frequência (30 a 200 BPM), e uma chave liga o modo "ataque cardíaco" (ritmo caótico com pausas). Usa as bibliotecas WiFi, PubSubClient, Wire e LiquidCrystal_I2C, e conecta à rede Wokwi-GUEST.
Componente	Pino (ESP32)	Função
Potenciômetro	GPIO 34 (ADC)	Ajusta o BPM alvo, com filtro para suavizar ruído
Chave deslizante	GPIO 27	Ativa o modo ataque cardíaco (nível LOW)
Botão verde	GPIO 14	Reconhece e silencia o alarme (debounce de 50 ms)
LED vermelho	GPIO 2	Pisca a cada batimento (pulso de 80 ms)
Relé	GPIO 26	Liga quando há alarme não reconhecido (simula sirene)
LCD 16x2 I2C (0x27)	SDA 21 / SCL 22	Mostra BPM, desvio padrão, estado e indicadores A (ataque), R (relé), M (MQTT)

Como o ritmo é analisado. O ESP32 guarda os últimos 10 intervalos entre batimentos (intervalos RR) e calcula a média, o desvio padrão (SD) e o coeficiente de variação (CV = SD / média). Com pelo menos 5 intervalos, classifica:
Estado	Critério
ASSISTOLIA	Nenhum batimento há mais de 3 s
ARRITMIA	CV maior que 0,15
TAQUICARDIA	BPM médio acima de 120
BRADICARDIA	BPM médio abaixo de 50
NORMAL	Nenhuma das condições acima

No modo normal, o intervalo é 60000 / BPM com variação natural de ±3%. No modo ataque, 8% dos intervalos são pausas de 3,2 a 4,5 s e o restante varia de 220 a 1300 ms, o que dispara arritmia e, nas pausas, assistolia.

´´´
4. Telemetria com MQTT
´´´
   
O ESP32 publica por MQTT no broker do Hive, que grava os valores nos campos de um canal e desenha um gráfico por campo em um painel em Python. 
Item	Valor
Broker	broker.hivemq.com
Portas	1883 (TCP), 8883 (TLS), 80 e 443 (WebSocket, caminho /mqtt)
Tópico de publicação	channels/<channelID>/publish
Formato da mensagem	Pares campo=valor separados por &, com field1 a field8 e status
QoS e retain	QoS 0, retain desligado

Mapeamento proposto dos campos. Os campos precisam estar habilitados nas configurações do canal. O status leva o estado classificado pelo ESP32, que cumpre o papel do alerta.
Campo	Dado	Descrição
field1	bpm	Média de batimentos por minuto, calculada com os últimos 10 intervalos. Vale 0 durante uma assistolia.
field2	sd_rr_ms	Desvio padrão dos intervalos entre batimentos, em milissegundos.
field3	cv	Coeficiente de variação, ou seja, o desvio padrão dividido pela média. Acima de 0,15 vira arritmia.
field4	bpm_ajustado	Valor escolhido no potenciômetro (de 30 a 200).
field5	amostras	Quantos intervalos entraram no cálculo (de 0 a 10).
field6	rele	1 quando o relé de alarme está ligado e 0 quando está desligado.
field7	modo	0 para normal e 1 para ataque (ativado pela chave).
field8	uptime_s	Segundos desde que o ESP32 ligou.
status	estado	AGUARDANDO, NORMAL, BRADICARDIA, TAQUICARDIA, ARRITMIA ou ASSISTOLIA.


´´´
5. Artigos de referência
´´´
   
Os três trabalhos usam ESP32 para monitorar sinais vitais e enviar dados à nuvem. Nenhum simula o sinal. O IJSREM e o TFG também usam o ThingSpeak, e só o TFG envia os dados a ele por MQTT, como este projeto.
Trabalho	Hardware	Protocolo e nuvem	Resultado principal
Krishnapriya e Prema (2025), Telehealth and Medicine Today
ESP32 DevKit V1 + sensor MAX30102 (frequência cardíaca e SpO₂)	Não detalhado no resumo	Acurácia relatada de 99,88% (frequência cardíaca) e 99,38% (SpO₂) em piloto pequeno; os autores pedem validação clínica mais ampla
Prasad et al. (2025), IJSREM
ESP32 + LM35, DHT11, sensor de pulso e sensor de ECG, com LCD 16x2	HTTP para o ThingSpeak, com gráficos em tempo real	Frequência cardíaca de 60 a 100 BPM em repouso; ruído no ECG e dependência da qualidade do Wi-Fi
Ribas e Oliveira, TFG (UFN)
ESP32 + MAX30100 (frequência cardíaca e SpO₂) e LM35 (temperatura)	MQTT para o ThingSpeak	Valores imprecisos de temperatura e instáveis de frequência cardíaca com movimento; como trabalho futuro, propõe alertas de risco

Ligação com o projeto. O TFG sugere como trabalho futuro justamente o que o ESP32 simulado já faz: classificar o sinal e disparar alerta de risco. Os artigos também mostram os problemas que a simulação evita (ruído do sensor e instabilidade do Wi-Fi) e que valem como limitações a citar.

´´´
6. Como executar e testar
´´´

1.	Baixe todas as dependências do Painel do Python no repositório de github, rode o programa e configure o broker com os mesmos dados que o Wokwi
2.	Rode o Projeto no Wokwi e configure as informações do broker
3.	No sketch do Wokwi, aplique as mudanças da seção 4 com os seus dados, ajuste o intervalo de envio para 15 s e inicie a simulação.
4.	Confirme no monitor serial a conexão Wi-Fi e a mensagem de MQTT conectado (o LCD mostra M quando conectado).
5.	Abra o Painel e veja os gráficos dos campos atualizando.
6.	Gire o potenciômetro: field1 (BPM) acompanha, e valores acima de 120 ou abaixo de 50 mudam o status para TAQUICARDIA ou BRADICARDIA.
7.	Mude a chave para ataque: field2 (desvio padrão) e field3 (CV) sobem, o status passa a ARRITMIA ou ASSISTOLIA e field6 (relé) vai a 1. Aperte o botão verde para silenciar o relé e volte a chave ao normal para o status voltar a NORMAL.
   
7. Fontes

https://github.com/profdanielrsousa/iot_e_aplicacoes/blob/main/ex20_esp32_SERVIDOR_MQTT_ADC.py
https://wokwi.com/projects/479872168818208769
https://telehealthandmedicinetoday.com/index.php/journal/article/view/611
https://ijsrem.com/download/smart-health-monitoring-system-with-esp32-real-time-data-cloud-based-reports/fulltext.pdf
https://tfgonline.lapinf.ufn.edu.br/media/midias/AndersonRibeiroRibas.pdf

