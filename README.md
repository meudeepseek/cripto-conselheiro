# 🤖 Bot Conselheiro v3.4 — Sistema Quantitativo Crypto Spot

Motor de monitoramento e alertas algorítmicos para operação **zero-custódia** na **Binance Spot**, gerenciado via Telegram e hospedado 24/7 no Render.

---

## ⚙️ Parâmetros Oficiais Validados

Após testes exaustivos em **5 anos de dados históricos reais (1.825 dias)** da Binance, descontando taxas de negociação (`0,1%` por ordem), a configuração campeã definida para produção é:

| Parâmetro | Configuração Oficial | Justificativa Matemática |
| :--- | :--- | :--- |
| **Cesta de Ativos** | `BTC`, `ETH`, `SOL`, `BNB`, `LINK` | Alta liquidez institucional; evita rompimentos falsos de altcoins menores. |
| **Timeframe** | `4h` (4 horas) | Filtra ruído intradiário mantendo agilidade na captura de tendências. |
| **Filtro de Tendência** | Preço > `EMA 200` | Bloqueia entradas durante *bear markets* e quedas prolongadas. |
| **Gatilho de Entrada** | `Breakout 50v` + Volume > Média 20v | Confirma rompimento de máxima de 50 velas (~8,3 dias) com força compradora. |
| **Stop Loss** | `3,0x ATR (14)` | Ajusta a proteção dinamicamente à volatilidade de cada moeda. |
| **Take Profit (Alvo)** | `1:2.5` (`7,5x ATR`) | Assimetria positiva: 1 vitória paga 2,5 derrotas. |
| **Gestão de Risco** | `2,5%` por trade (teto `40%` banca/ordem) | Maximiza juros compostos mantendo o *drawdown* controlado. |
| **Exposição Máxima** | Máximo `2` posições simultâneas | Evita risco sistêmico de quedas sincronizadas no mercado cripto. |

---

## 📊 Performance Histórica Mês a Mês (2021–2026)

Simulação com banca inicial de **1.000,00 USDT** e reinvestimento composto (sem considerar aportes externos):

| Ano | Jan | Fev | Mar | Abr | Mai | Jun | Jul | Ago | Set | Out | Nov | Dez | **Total Ano** | **Max DD** | **Acerto** |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **2021** | — | — | — | — | — | — | — | — | — | — | — | -4,2% | **-4,19%** | -4,19% | 0,0% *(2)* |
| **2022** | 0,0% | -5,2% | +9,0% | -5,8% | 0,0% | 0,0% | -1,7% | +0,4% | -7,1% | +7,8% | -2,1% | -6,1% | **-11,44%** | -21,32% | 26,9% *(26)* |
| **2023** | **+27,6%** | -9,1% | +7,7% | +2,7% | -6,8% | +2,1% | -2,3% | -5,4% | +10,0% | **+41,3%** | -1,3% | +14,5% | **+98,01%** | -16,66% | 53,8% *(52)* |
| **2024** | -5,9% | **+32,4%** | +10,3% | -16,3% | +7,7% | -7,0% | +8,0% | -7,3% | +2,0% | -1,7% | +11,5% | -2,6% | **+25,65%** | -19,80% | 38,5% *(52)* |
| **2025** | +2,7% | -4,7% | -3,6% | +6,4% | **+15,7%** | +0,7% | +10,0% | +10,2% | +5,9% | -6,6% | 0,0% | -5,1% | **+33,09%** | -16,70% | 38,6% *(44)* |
| **2026** | +3,1% | 0,0% | -6,4% | +3,9% | +1,9% | -5,0% | +5,0% | **+27,3%** | +7,6% | -8,0% | -2,1% | — | **+25,67%** | -12,25% | 44,4% *(27)* |

* **Retorno Acumulado (5 anos):** **`+253,13%`** (`1.000,00 USDT` $\rightarrow$ `3.531,28 USDT`)
* **Total de Operações:** `203 trades` (média de ~3,4 operações/mês) | **Taxa de Acerto Global:** `41,4%`
* **Pior Drawdown Histórico:** `-21,32%` (durante o *bear market* de 2022, quando o mercado à vista caiu mais de 70%)

---

## 🧪 Resumo dos Testes de Estresse e Arquitetura

Antes de congelar a versão `v3.4`, todas as variações alternativas foram submetidas ao mesmo histórico de 5 anos e descartadas por apresentarem pior relação retorno/risco:

| Experimento Testado | Retorno (5 Anos) | Max Drawdown | Conclusão |
| :--- | :---: | :---: | :--- |
| **Configuração Oficial (`v3.4`)** | **`+253,13%`** | **`-21,32%`** | **Campeã em eficiência e proteção de capital.** |
| Risco dobrado (`5,0%` por trade) | `+334,98%` | `-30,59%` | Retorno marginal não compensa dobrar o risco por ordem. |
| Aumentar para `4` vagas simultâneas | `+132,62%` | `-33,44%` | Exposição excessiva a quedas sincronizadas derruba o lucro. |
| Gatilho rápido (`Breakout 20v`) | `+62,45%` | `-47,06%` | Excesso de sinais falsos (*overtrading*) corrói a banca. |
| Alvo esticado (`1:3.5` + `6%` risco) | `+36,07%` | `-51,14%` | Taxa de acerto cai para `27,2%` e corta a banca pela metade. |
| Expandir cesta para `10` moedas | `+116,75%` | `-32,97%` | Altcoins secundárias ocupam vagas com rompimentos falsos. |
| Expandir cesta para `14` moedas | `+48,03%` | `-35,90%` | Ruído excessivo derruba o acerto para `34,3%`. |
| Rotação Momentum Semanal (Top 2) | `+91,95%` | `-59,21%` | Giro semanal gera taxas altas e tombos fortes sem Stop ATR. |
| Combo Híbrido (Breakout + `RSI < 25`) | `-21,22%` | `-54,74%` | Comprar fundo em pânico ("faca caindo") consome o lucro do Breakout. |
| Reversão de Pânico pura (`RSI < 25`) | `-76,27%` | `-76,80%` | Inviável em tendências de baixa prolongadas. |

---

## 📱 Comandos de Operação (Telegram)

* `/status` — Verifica saúde do servidor, banca configurada e vagas ocupadas (`0/2`).
* `/radar` — Consulta preço atual, `EMA 200`, distância para o `Breakout 50v` e `ATR` das 5 moedas.
* `/banca <valor>` — Atualiza o saldo total em USDT após aportes mensais ou fechamento de ciclos.
* `/abrir <MOEDA>` — Registra entrada executada na Binance Spot e ocupa 1 vaga.
* `/fechar <MOEDA>` — Libera a vaga após execução da ordem OCO (Take Profit ou Stop Loss).
* `/posicoes` — Lista as posições em andamento monitoradas pelo bot.
