# Trading Journal Data API

매매 복기 도구(Trading Journal)의 차트 데이터 서버.

한국 주식 일봉(OHLCV) 데이터를 [pykrx](https://github.com/sharebook-kr/pykrx)로 가져와 프론트엔드에 JSON으로 제공합니다.

## 엔드포인트

- `GET /health` — 헬스체크
- `GET /api/ohlcv/{ticker}?days=365` — 종목 일봉 데이터 (예: `/api/ohlcv/005930?days=180`)
- `GET /api/name/{ticker}` — 종목명 조회

## 배포 (Render)

이 저장소를 Render Web Service로 연결하면 `render.yaml`을 인식해 자동 배포됩니다.

수동 설정 시:
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Plan: Free

## 로컬 실행 (선택)

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

http://localhost:8000/health 에서 확인.

## 캐시

요청은 메모리에 1시간 캐시됩니다. cold start(15분 비활성 후) 시 캐시 초기화.
