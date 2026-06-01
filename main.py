"""
Trading Journal Data API
한국 주식 일봉 데이터(OHLCV)를 pykrx로 가져와 프론트엔드에 제공.

엔드포인트:
  GET  /                                  - 헬스체크
  GET  /health                            - 헬스체크 (UptimeRobot용)
  GET  /api/ohlcv/{ticker}?days=365       - 종목 일봉 데이터 (최근 N일)
  GET  /api/ohlcv/{ticker}?from=&to=      - 종목 일봉 데이터 (특정 기간)
  GET  /api/name/{ticker}                 - 종목명 조회
  GET  /api/close/{ticker}                - 종목 최신 종가 (보유 종목 미실현용)
  POST /api/closes                        - 여러 종목 최신 종가 배치
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pykrx import stock
from datetime import datetime, timedelta
from typing import List, Optional
import time

app = FastAPI(title="Trading Journal Data API", version="0.2.0")

# CORS 설정: v1 단계엔 모든 도메인 허용. 친구 공유 단계에서 도메인 제한 추가.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "trading-journal-data-api",
        "status": "ok",
        "version": "0.2.0",
        "endpoints": [
            "/health",
            "/api/ohlcv/{ticker}?days=365",
            "/api/ohlcv/{ticker}?from=YYYY-MM-DD&to=YYYY-MM-DD",
            "/api/name/{ticker}",
            "/api/close/{ticker}",
            "/api/closes (POST body: {\"tickers\":[\"005930\",...]})",
        ],
    }


@app.head("/health")
@app.get("/health")
def health():
    """헬스체크 — UptimeRobot이 5분마다 호출해서 슬립 방지"""
    return {"status": "ok"}


# ----- 메모리 캐시 -----
# pykrx 호출은 KRX 사이트 응답 대기 때문에 느림(2~5초). 메모리에 캐시.
_CACHE_TTL_LONG = 3600       # 일봉/종목명: 1시간
_CACHE_TTL_SHORT = 300       # 최신 종가: 5분
_ohlcv_cache: dict = {}
_name_cache: dict = {}
_close_cache: dict = {}


def _get_cached(cache: dict, key, ttl: int = _CACHE_TTL_LONG):
    entry = cache.get(key)
    if not entry:
        return None
    ts, data = entry
    if time.time() - ts > ttl:
        return None
    return data


def _set_cache(cache: dict, key, data, max_size: int = 200):
    cache[key] = (time.time(), data)
    if len(cache) > max_size:
        oldest = min(cache.keys(), key=lambda k: cache[k][0])
        cache.pop(oldest, None)


def _normalize_ticker(ticker: str) -> str:
    """종목코드 정규화.
    
    한국 종목: 일반 주식은 숫자 6자리 (예: 005930 삼성전자)
    ETF/일부 신규 종목: 영문 섞인 6자리 코드도 있음 (예: 0195R0)
    """
    t = (ticker or "").strip().upper()
    # 숫자만이면 6자리 0-padding (예: 5930 → 005930)
    if t.isdigit():
        return t.zfill(6)
    # 영숫자 6자리 (ETF 등)
    if len(t) == 6 and t.isalnum():
        return t
    raise HTTPException(
        status_code=400,
        detail=f"종목코드 형식 오류: '{ticker}' (숫자 6자리 또는 영숫자 6자리만 허용)"
    )


def _parse_date(s: str) -> str:
    """YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD → YYYYMMDD"""
    if not s:
        return ""
    s = s.strip().replace("-", "").replace("/", "")
    if not (len(s) == 8 and s.isdigit()):
        raise HTTPException(status_code=400, detail=f"날짜 형식 오류: {s} (YYYY-MM-DD 형식)")
    return s


@app.get("/api/ohlcv/{ticker}")
def get_ohlcv(
    ticker: str,
    days: Optional[int] = Query(None, ge=1, le=3650, description="가져올 일수 (from/to 미지정시)"),
    from_date: Optional[str] = Query(None, alias="from", description="시작일 YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, alias="to", description="종료일 YYYY-MM-DD (기본 오늘)"),
):
    """종목 일봉 데이터(OHLCV) 조회

    두 가지 모드:
    1. days만 지정: 오늘 기준 최근 N일
       예: /api/ohlcv/005930?days=365
    2. from/to 지정: 특정 기간 (매도 후 추적용)
       예: /api/ohlcv/005930?from=2025-04-01&to=2025-05-01

    반환:
    {
        "ticker": "005930",
        "name": "삼성전자",
        "from": "20250401",
        "to": "20250501",
        "candles": [
            {"date":"2025-04-01","open":70000,"high":72000,"low":69500,
             "close":71500,"volume":12345678},
            ...
        ],
        "fetched_at": "2025-05-18T..."
    }
    """
    ticker = _normalize_ticker(ticker)

    # 모드 결정
    if from_date or to_date:
        if not from_date:
            raise HTTPException(status_code=400, detail="from 파라미터가 필요합니다")
        start_str = _parse_date(from_date)
        end_str = _parse_date(to_date) if to_date else datetime.now().strftime("%Y%m%d")
        cache_key = (ticker, "range", start_str, end_str)
    else:
        d = days or 365
        end_date = datetime.now()
        start_date = end_date - timedelta(days=d)
        start_str = start_date.strftime("%Y%m%d")
        end_str = end_date.strftime("%Y%m%d")
        cache_key = (ticker, "days", d)

    cached = _get_cached(_ohlcv_cache, cache_key)
    if cached:
        return cached

    try:
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
            "from": start_str,
            "to": end_str,
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


# ===== 최신 종가 (보유 종목 미실현 손익용) =====

def _fetch_latest_close(ticker: str) -> dict:
    """단일 종목 최신 종가. 토/일/공휴일이면 직전 영업일 종가."""
    ticker = _normalize_ticker(ticker)

    cached = _get_cached(_close_cache, ticker, ttl=_CACHE_TTL_SHORT)
    if cached:
        return cached

    # 직전 10일 범위로 조회 (휴장 안전망). 가장 마지막 candle = 최신 종가.
    end_date = datetime.now()
    start_date = end_date - timedelta(days=10)
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    df = stock.get_market_ohlcv(start_str, end_str, ticker)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"종목 {ticker} 종가 데이터 없음")

    last_row = df.iloc[-1]
    last_date = df.index[-1]
    last_close = int(last_row["종가"])

    # 전일 종가 (등락률 계산용)
    prev_close = int(df.iloc[-2]["종가"]) if len(df) >= 2 else None
    change = (last_close - prev_close) if prev_close is not None else None
    change_pct = (change / prev_close * 100) if (prev_close and prev_close > 0) else None

    result = {
        "ticker": ticker,
        "date": last_date.strftime("%Y-%m-%d"),
        "close": last_close,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
    }
    _set_cache(_close_cache, ticker, result, max_size=500)
    return result


@app.get("/api/close/{ticker}")
def get_latest_close(ticker: str):
    """단일 종목 최신 종가. 보유 카드 미실현 손익 계산용.

    토/일/공휴일 호출 시 직전 영업일 종가 반환.

    반환:
    {
        "ticker": "005930",
        "date": "2025-05-17",
        "close": 71500,
        "prev_close": 71000,
        "change": 500,
        "change_pct": 0.7042
    }
    """
    try:
        return _fetch_latest_close(ticker)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"종가 조회 실패: {type(e).__name__}: {str(e)}",
        )


class ClosesRequest(BaseModel):
    tickers: List[str]


@app.post("/api/closes")
def get_latest_closes(body: ClosesRequest):
    """여러 종목 최신 종가 한 번에 조회.

    Body: { "tickers": ["005930", "000660", "035420"] }

    각 종목별로 캐시 활용. 일부 실패해도 나머지는 정상 반환.

    반환:
    {
        "results": [{ticker, date, close, prev_close, change, change_pct}, ...],
        "errors":  [{"ticker": "...", "error": "..."}],
        "fetched_at": "..."
    }
    """
    results = []
    errors = []
    for raw in (body.tickers or []):
        try:
            data = _fetch_latest_close(raw)
            results.append(data)
        except HTTPException as e:
            errors.append({"ticker": raw, "error": str(e.detail)})
        except Exception as e:
            errors.append({"ticker": raw, "error": f"{type(e).__name__}: {str(e)}"})
    return {
        "results": results,
        "errors": errors,
        "fetched_at": datetime.now().isoformat(),
    }
