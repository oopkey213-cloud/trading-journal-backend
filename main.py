"""
Trading Journal Data API
한국 주식 일봉 데이터(OHLCV)를 pykrx로 가져와 프론트엔드에 제공.

엔드포인트:
  GET /                            - 헬스체크
  GET /health                      - 헬스체크 (UptimeRobot용)
  GET /api/ohlcv/{ticker}?days=365 - 종목 일봉 데이터
  GET /api/name/{ticker}           - 종목명 조회
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pykrx import stock
from datetime import datetime, timedelta
import time

app = FastAPI(title="Trading Journal Data API", version="0.1.0")

# CORS 설정: v1 단계엔 모든 도메인 허용. 친구 공유 단계에서 도메인 제한 추가.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "trading-journal-data-api",
        "status": "ok",
        "version": "0.1.0",
        "endpoints": [
            "/health",
            "/api/ohlcv/{ticker}?days=365",
            "/api/name/{ticker}",
        ],
    }


@app.head("/health")
@app.get("/health")
def health():
    """헬스체크 — UptimeRobot이 5분마다 호출해서 슬립 방지"""
    return {"status": "ok"}


# ----- 메모리 캐시 -----
# pykrx 호출은 KRX 사이트 응답 대기 때문에 느림(2~5초). 메모리에 1시간 캐시.
_CACHE_TTL_SEC = 3600
_ohlcv_cache: dict = {}  # key=(ticker, days) -> (timestamp, data)
_name_cache: dict = {}   # key=ticker -> (timestamp, name)


def _get_cached(cache: dict, key):
    entry = cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > _CACHE_TTL_SEC:
        return None
    return data


def _set_cache(cache: dict, key, data, max_size=200):
    cache[key] = (time.time(), data)
    if len(cache) > max_size:
        oldest = min(cache.keys(), key=lambda k: cache[k][0])
        cache.pop(oldest, None)


def _normalize_ticker(ticker: str) -> str:
    """종목코드를 6자리 0-padding 숫자로 정규화"""
    t = (ticker or "").strip()
    if not t.isdigit():
        raise HTTPException(status_code=400, detail="종목코드는 숫자여야 합니다 (예: 005930)")
    return t.zfill(6)


@app.get("/api/ohlcv/{ticker}")
def get_ohlcv(
    ticker: str,
    days: int = Query(365, ge=1, le=3650, description="가져올 일수 (기본 365일, 최대 10년)"),
):
    """종목 일봉 데이터(OHLCV) 조회

    예: /api/ohlcv/005930?days=365  -> 삼성전자 최근 1년

    반환:
    {
        "ticker": "005930",
        "name": "삼성전자",
        "days": 365,
        "candles": [
            {"date":"2024-11-05","open":70000,"high":72000,"low":69500,
             "close":71500,"volume":12345678},
            ...
        ],
        "fetched_at": "2025-05-18T..."
    }
    """
    ticker = _normalize_ticker(ticker)

    cache_key = (ticker, days)
    cached = _get_cached(_ohlcv_cache, cache_key)
    if cached:
        return cached

    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")

        df = stock.get_market_ohlcv(start_str, end_str, ticker)

        if df is None or df.empty:
            raise HTTPException(
                status_code=404,
                detail=f"종목 {ticker} 데이터 없음 — 코드 확인 필요",
            )

        # 종목명
        name = ""
        try:
            name = stock.get_market_ticker_name(ticker) or ""
        except Exception:
            pass

        candles = []
        for date, row in df.iterrows():
            candles.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": int(row["시가"]),
                "high": int(row["고가"]),
                "low": int(row["저가"]),
                "close": int(row["종가"]),
                "volume": int(row["거래량"]),
            })

        result = {
            "ticker": ticker,
            "name": name,
            "days": days,
            "candles": candles,
            "fetched_at": datetime.now().isoformat(),
        }

        _set_cache(_ohlcv_cache, cache_key, result)
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"데이터 가져오기 실패: {type(e).__name__}: {str(e)}",
        )


@app.get("/api/name/{ticker}")
def get_ticker_name(ticker: str):
    """종목코드로 종목명만 빠르게 조회"""
    ticker = _normalize_ticker(ticker)

    cached = _get_cached(_name_cache, ticker)
    if cached:
        return cached

    try:
        name = stock.get_market_ticker_name(ticker) or ""
        result = {"ticker": ticker, "name": name}
        _set_cache(_name_cache, ticker, result, max_size=1000)
        return result
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"종목명 조회 실패: {type(e).__name__}: {str(e)}",
        )
