# 한국 시장 데이터 번들 v1.0

> 이 형식은 연결·연구용 기본 번들이다. 상장폐지와 과거 지정상태까지 입증하는 실거래 자격 검증에는 [DATA_QUALIFICATION.md](./DATA_QUALIFICATION.md)의 schema 2.0을 사용한다.

MoneyGun은 KRX의 비공식 다운로드 URL을 자동 호출하지 않는다. 사용자가 내부 연구 이용 권한을 확인한 데이터만 `MONEYGUN_MARKET_DATA_IMPORT_DIR`에 넣고 내용 주소형 스냅샷으로 가져온다. 원본과 이용 조건은 별도로 보존한다.

## 최소 형식

```json
{
  "schema_version": "1.0",
  "as_of": "2026-08-31T15:40:00+09:00",
  "source": "KRX_AUTHORIZED_EXPORT",
  "license_basis": "USER_AUTHORIZED_INTERNAL_RESEARCH",
  "benchmark": { "symbol": "KOSPI200", "bars": [] },
  "instruments": [
    {
      "symbol": "005930",
      "name": "종목명",
      "market": "KOSPI",
      "sector": "업종",
      "bars": [],
      "fundamentals": {},
      "fundamentals_history": [
        { "effective_at": "2026-05-15T08:00:00+09:00", "values": {} }
      ],
      "catalyst_evidence_ids": [],
      "customer_concentration": "UNKNOWN"
    }
  ],
  "evidence": []
}
```

각 `bars` 항목은 `date`, `open`, `high`, `low`, `close`, `volume`을 가진다. 전략 계산에는 최소 200거래일, R0→R1 검토에는 공통 1,260거래일 이상이 필요하다. 재무정보는 공시 후 실제로 사용할 수 있었던 `effective_at`, 공시는 `published_at`을 보존해야 한다.

허용 source는 `KRX_AUTHORIZED_EXPORT`, `LICENSED_VENDOR`, 허용 권한 표시는 `USER_AUTHORIZED_INTERNAL_RESEARCH`, `OFFICIAL_DATA_CONTRACT`다. 권한 표시만으로 실제 라이선스가 생기지는 않으며 사용자가 원 계약을 확인해야 한다.

## 가져오기와 검증

1. `.env`의 `MONEYGUN_MARKET_DATA_IMPORT_DIR`에 파일을 둔다.
2. `POST /v1/research/snapshots/import`에 `{ "file_name": "bundle.json" }`와 `Idempotency-Key`를 보낸다.
3. 반환된 `snapshot_id`로 `IMPORTED_SNAPSHOT` 연구 사이클을 실행한다.
4. `POST /v1/research/validations/run`으로 OOS 게이트를 계산한다.

경로 이탈, 50MB 초과, timezone 누락, 승인되지 않은 source·권한 표시, 미래 봉·재무·근거, 품질 실패는 모두 가져오기 전에 차단한다. 실제 주문은 이 과정과 무관하게 비활성 상태다.
