import os
import time
import json
import logging
from datetime import datetime
from typing import List, Tuple

import ccxt
import pandas as pd
import requests
import feedparser
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# ==============================================================================
# 1. CONFIGURAÇÃO DE LOGS E VARIÁVEIS DE AMBIENTE
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY]):
    raise ValueError("ERRO CRÍTICO: Verifique se TELEGRAM_TOKEN, TELEGRAM_CHAT_ID e GEMINI_API_KEY estão no arquivo .env")

# Parâmetros da Estratégia
PARES = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "LINK/USDT"]
TIMEFRAME = "1h"
RSI_LIMITE_COMPRA = 30.0
MULTIPLICADOR_ATR = 1.5
MAX_STOP_PERCENT = 0.045
RELACAO_RISCO_RETORNO = 2.0

# Fontes RSS gratuitas e confiáveis
FEEDS_RSS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/"
]

# Inicialização dos Clientes
exchange = ccxt.binance({"enableRateLimit": True, "timeout": 15000})
ai_client = genai.Client(api_key=GEMINI_API_KEY)
ultimo_alerta = {}


# ==============================================================================
# 2. SCHEMA ESTRITO PARA A RESPOSTA DA IA (EVITA ALUCINAÇÃO)
# ==============================================================================
class AnaliseRiscoIA(BaseModel):
    vetar_operacao: bool = Field(
        description="True APENAS se houver notícia grave/catastrófica (hack, falência, processo SEC, bug crítico) afetando a moeda ou o mercado cripto."
    )
    nivel_risco: str = Field(
        description="Deve ser estritamente: BAIXO, MEDIO ou ALTO"
    )
    resumo_noticias: str = Field(
        description="Resumo objetivo em português (máximo 2 frases) do contexto atual das notícias."
    )


# ==============================================================================
# 3. COLETA DE NOTÍCIAS E FILTRO DE VETO COM IA
# ==============================================================================
def coletar_manchetes(limite_por_feed: int = 8) -> List[str]:
    """Coleta manchetes recentes dos feeds RSS com proteção contra falhas de rede."""
    manchetes = []
    for url in FEEDS_RSS:
        try:
            # Usa requests com timeout antes de passar para o feedparser (evita travamento infinito)
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:limite_por_feed]:
                titulo = entry.get("title", "").strip()
                if titulo:
                    manchetes.append(f"- {titulo}")
        except Exception as e:
            logging.warning(f"Falha ao ler feed RSS ({url}): {e}")
    return manchetes


def consultar_ia_veto(par: str, preco: float, rsi: float) -> Tuple[bool, str, str]:
    """
    Consulta a IA para atuar EXCLUSIVAMENTE como filtro de veto.
    Retorna: (vetar: bool, nivel_risco: str, resumo: str)
    """
    manchetes = coletar_manchetes()
    if not manchetes:
        return False, "DESCONHECIDO (RSS Offline)", "Não foi possível carregar notícias. Valide manualmente."

    moeda_base = par.split("/")[0]
    texto_manchetes = "\n".join(manchetes)

    prompt = f"""
    Você é um analista de risco quantitativo de criptomoedas.
    O sistema técnico detectou uma oportunidade de compra (sobrevenda) para o par {par} (Preço: ${preco:.4f}, RSI: {rsi:.1f}).
    
    Analise as manchetes recentes abaixo e verifique se existe algum evento catastrófico, hack, golpe, problema regulatório grave ou colapso sistêmico acontecendo AGORA que justifique VETAR (cancelar) essa compra.
    Quedas normais de preço sem notícia catastrófica NÃO devem ser vetadas.
    
    Manchetes recentes:
    {texto_manchetes}
    """

    try:
        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnaliseRiscoIA,
                temperature=0.1,  # Temperatura baixa para máxima objetividade
            ),
        )
        resultado = json.loads(response.text)
        return (
            bool(resultado["vetar_operacao"]),
            str(resultado["nivel_risco"]),
            str(resultado["resumo_noticias"])
        )
    except Exception as e:
        logging.error(f"Falha na consulta à API da IA: {e}")
        # Fail-Safe: Não bloqueia o alerta técnico, mas avisa o humano que a IA falhou
        return False, "IA OFFLINE", "Erro ao consultar IA. Verifique notícias manualmente antes de operar."


# ==============================================================================
# 4. INDICADORES MATEMÁTICOS E TELEGRAM
# ==============================================================================
def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    delta = df["close"].diff()
    ganho = delta.clip(lower=0)
    perda = -delta.clip(upper=0)

    media_ganho = ganho.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    media_perda = perda.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

    # Proteção contra divisão por zero caso media_perda seja 0
    rs = media_ganho / media_perda.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()

    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    df["vol_medio"] = df["volume"].rolling(window=20).mean()

    return df


def enviar_telegram(mensagem: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensagem, "parse_mode": "HTML"}
    for tentativa in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return
        except Exception as e:
            logging.warning(f"Tentativa {tentativa + 1}/3 falhou no Telegram: {e}")
            time.sleep(2)


# ==============================================================================
# 5. MOTOR DE ANÁLISE PRINCIPAL
# ==============================================================================
def analisar_par(par: str) -> None:
    try:
        velas = exchange.fetch_ohlcv(par, timeframe=TIMEFRAME, limit=100)
        if not velas or len(velas) < 30:
            logging.warning(f"[{par}] Dados insuficientes retornados pela Binance.")
            return

        df = pd.DataFrame(velas, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df = calcular_indicadores(df)

        vela_fechada = df.iloc[-2]
        preco_atual = float(df.iloc[-1]["close"])
        rsi = float(vela_fechada["rsi"])
        atr = float(vela_fechada["atr"])
        volume_atual = float(vela_fechada["volume"])
        volume_medio = float(vela_fechada["vol_medio"])

        if pd.isna(rsi) or pd.isna(atr) or preco_atual <= 0:
            return

        # Gatilho Técnico
        if rsi < RSI_LIMITE_COMPRA and volume_atual > (volume_medio * 0.8):
            agora = time.time()
            if par in ultimo_alerta and (agora - ultimo_alerta[par]) < 14400:
                return

            distancia_stop = atr * MULTIPLICADOR_ATR
            stop_loss = preco_atual - distancia_stop
            stop_percentual = distancia_stop / preco_atual

            if stop_percentual > MAX_STOP_PERCENT:
                logging.info(f"[{par}] Descartado: Stop técnico muito longo ({stop_percentual*100:.2f}%)")
                return

            # Aciona a IA apenas quando o sinal técnico é válido (economiza API)
            logging.info(f"[{par}] Sinal técnico detectado (RSI: {rsi:.1f}). Consultando filtro de veto IA...")
            vetar, nivel_risco, resumo_ia = consultar_ia_veto(par, preco_atual, rsi)

            if vetar:
                logging.warning(f"[{par}] OPERAÇÃO VETADA PELA IA! Motivo: {resumo_ia}")
                enviar_telegram(
                    f"🛑 <b>SINAL VETADO PELA IA: {par}</b>\n"
                    f"• RSI atingiu <code>{rsi:.1f}</code>, mas a IA detectou risco elevado.\n"
                    f"• <b>Motivo:</b> {resumo_ia}"
                )
                ultimo_alerta[par] = agora
                return

            take_profit = preco_atual + (distancia_stop * RELACAO_RISCO_RETORNO)
            alvo_percentual = stop_percentual * RELACAO_RISCO_RETORNO

            mensagem = (
                f"🚨 <b>OPORTUNIDADE APROVADA: {par}</b>\n\n"
                f"📊 <b>Dados Técnicos ({TIMEFRAME}):</b>\n"
                f"• Preço Atual: <code>${preco_atual:.4f}</code>\n"
                f"• RSI (14): <code>{rsi:.1f}</code>\n\n"
                f"🧠 <b>Filtro de Risco IA ({nivel_risco}):</b>\n"
                f"• {resumo_ia}\n\n"
                f"🛡️ <b>Parâmetros de Gestão (1:2):</b>\n"
                f"• Stop Loss: <code>${stop_loss:.4f}</code> (-{stop_percentual*100:.2f}%)\n"
                f"• Take Profit: <code>${take_profit:.4f}</code> (+{alvo_percentual*100:.2f}%)\n\n"
                f"✅ <i>Valide no gráfico e configure ordem OCO no Spot.</i>"
            )

            enviar_telegram(mensagem)
            ultimo_alerta[par] = agora
            logging.info(f"[{par}] Alerta aprovado enviado com sucesso!")

    except ccxt.NetworkError as e:
        logging.warning(f"[{par}] Erro de rede temporário na Binance: {e}")
    except Exception as e:
        logging.error(f"[{par}] Erro inesperado ao analisar par: {e}", exc_info=True)


# ==============================================================================
# 6. LOOP COM PROTEÇÃO CONTRA QUEDAS
# ==============================================================================
if __name__ == "__main__":
    logging.info("Iniciando Bot Conselheiro v2 (Com Filtro de Veto IA)...")
    enviar_telegram("🤖 <b>Bot Conselheiro v2 iniciado!</b> Monitoramento técnico + IA de veto ativos.")

    while True:
        try:
            for par in PARES:
                analisar_par(par)
                time.sleep(1.5)
            time.sleep(300)
        except KeyboardInterrupt:
            logging.info("Bot encerrado manualmente pelo usuário.")
            break
        except Exception as e:
            logging.critical(f"Erro crítico no loop principal: {e}. Reiniciando ciclo em 60s...", exc_info=True)
            time.sleep(60)