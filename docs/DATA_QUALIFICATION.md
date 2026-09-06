# 한국 주식 실거래 자격 데이터

최종 갱신: 2026-09-04

## 목적

현재 키움 공식 스냅샷은 실제 가격이지만 현재 살아 있는 8종목만 담는다. 이 결과는 연결 확인과 연구 실행에는 쓸 수 있어도, 상장폐지 종목과 과거 투자유의 상태가 빠져 있어 실거래 자격 증거가 될 수 없다.

Signal Guild의 schema `2.0` 자격 번들은 다음 세 원본을 함께 고정한다.

1. 상장폐지 종목을 포함한 KOSPI·KOSDAQ 보통주 일봉
2. 종목별 상장일·상장폐지일 생애주기
3. 투자주의·경고·위험, 거래정지, 관리종목, 단기과열 등 적용 기간

공식 다운로드 시작점은 [KRX 정보데이터시스템](https://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd)과 [KIND 상세검색](https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain)이다. Signal Guild는 KIND 화면이 사용하는 공개 검색·Excel 흐름만 호출하며 요청 구간, 페이지, 원본 SHA-256을 모두 보존한다. 로그인 뒤에만 제공되는 KRX `전종목 지정내역`을 우회하지 않는다.

## 가장 쉬운 권장 경로

KRX 화면에서 거래일마다 파일을 반복 다운로드하지 않는다. 가격과 상장 생애주기는 KRX 자료를 제공하는 공공데이터포털의 공식 API로 자동 수집하고, API가 제공하지 않는 과거 투자유의 상태만 KIND 화면에서 내려받는다.

### 1. 공공데이터 API 두 개 신청

공공데이터포털에 로그인한 뒤 아래 페이지에서 각각 `활용신청`을 누른다. 자동승인 API이므로 일반적으로 즉시 개발계정 서비스키가 발급된다.

1. [금융위원회 주식시세정보](https://www.data.go.kr/data/15094808/openapi.do): 종목별 날짜·시가·고가·저가·종가·거래량 수집
2. [금융위원회 주식발행정보](https://www.data.go.kr/dataset/15043423/openapi.do): 종목코드·상장일·상장폐지일·발행주식수 등 생애주기 수집

활용 목적은 `개인 비공개 투자 연구 및 백테스트`처럼 실제 용도대로 적는다. 서비스키는 채팅에 보내지 않고 공공데이터포털 `마이페이지 → OpenAPI → 개발계정`에서 복사해 Git에서 제외된 로컬 `.env`에만 보관한다. 어댑터 구현 시 사용할 환경변수 이름은 `DATA_GO_KR_SERVICE_KEY`로 고정한다.

이 API로 2021-09-01부터 2026-08-31까지 자동 수집을 우선 시도한다. 상장폐지 종목의 과거 가격까지 반환되는지는 실제 키로 표본 검증하고, 빠진다면 해당 구간만 KRX 공식 export 또는 계약 데이터로 보완한다.

### 2. KIND 시장조치 이력 위치

[KIND 상세검색](https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain)의 `상세조건 → 시장조치`에서 아래 항목을 선택한다.

1. `투자주의종목` — 검색 코드 `0341`
2. `투자경고종목` — 검색 코드 `0342`
3. `투자위험종목` — 검색 코드 `0343`
4. `관리종목` — 검색 코드 `0350`
5. `투자주의환기종목` — 검색 코드 `0356`
6. `매매거래정지 및 정지해제` — 검색 코드 `0311`

Signal Guild는 이 공식 화면의 1년 조회 제한과 페이지당 100건 조건을 지키며 전체 페이지 Excel을 자동 보존한다. 수동으로 찾을 때는 시장 `전체`, 회사명 공란, 기간 1년 이하로 조회한다. KRX에서 현재 상태를 확인할 수 있는 공식 화면은 [매매거래정지](https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT213.jsp), [관리종목](https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT215.jsp), [투자주의환기종목](https://data.krx.co.kr/contents/MDC/STAT/issue/MDCSTAT218.jsp)이다.

```text
data/imports/raw/kind/
  warning-risk-2021-2026.*
  market-actions/
    caution-YYYYMMDD-YYYYMMDD-pNNN.xls
    warning-YYYYMMDD-YYYYMMDD-pNNN.xls
    danger-YYYYMMDD-YYYYMMDD-pNNN.xls
    managed-YYYYMMDD-YYYYMMDD-pNNN.xls
    watchlist-YYYYMMDD-YYYYMMDD-pNNN.xls
    halted-YYYYMMDD-YYYYMMDD-pNNN.xls
    manifest.json
```

현재 여섯 이력은 모두 자동 수집하므로 사용자가 추가 다운로드할 일은 없다. 과거 수동 파일은
감사 원본으로 보존하지만, 새 일일 번들은 기준일별 KIND 상세검색 6종 전체 이력을 사용한다.

## 일일 자동 갱신

`DESKTOP_LIVE` Supervisor는 `daily_market_refresh_scheduler`를 감시한다. 스케줄러는 거래일
18:30 이후 본 실행과 다음 날 06:00~08:20 보충 실행을 하루 한 번씩 요청한다. API는 즉시
작업 접수만 반환하고 김데이터 작업 스레드가 아래 단계를 수행한다.

1. 공공데이터 주식시세 API에서 실제 게시된 최근 완료 거래일을 찾는다.
2. 해당 기준일의 독립 폴더 `data/imports/daily-refresh/YYYYMMDD`에 5년 원본을 수집한다.
3. KIND 6종 이력, 가격, 발행·상장폐지 정보와 KODEX 200 1,200거래일 이상을 검사한다.
4. 정규화 DB와 schema 2.0 압축 번들을 만들고 기존 validator로 다시 전수검사한다.
5. PASS일 때만 불변 스냅샷을 저장한다. 실패 파일은 활성 데이터로 승격하지 않는다.

운영센터에는 `김데이터 일일 자동 갱신` 카드가 진행 단계, 최근 성공 기준일과 오류를 표시한다.
`지금 다시 갱신`은 데이터 수집만 다시 요청하며 주문을 만들거나 제출하지 않는다. PC 종료로
중단된 실행은 `INTERRUPTED`로 보이고 다음 자동 시간대에 재시도한다.

### 2026-08-31 실제 수령·연결 결과

KIND 원본 9개를 `data/imports/raw/kind`에 복사하고 SHA-256을 계산했다. 원본은 OLE Excel이 아니라 CP949 HTML 표를 `.xls` 확장자로 제공한 형식이므로 HTML 표 전용 파서가 필요하다.

- 투자경고 2개: 지정일·해제일 포함, 총 1,521행
- 투자위험 2개: 지정일·해제일 포함, 총 129행
- 기존 투자주의 2개: 각각 정확히 3,000행에서 잘려 전체 요청 기간을 포함하지 않음
- 관리종목·투자주의환기: 지정일은 있으나 해제일 없음
- 매매거래정지: 사유는 있으나 정지일·해제일 열 없음

3,000행 제한을 피하기 위해 2021-09-01부터 2026-08-31까지 투자주의를 20개 분기로 다시 내려받고, 시작 경계 `2021-08-28~2021-08-31` 한 파일을 추가해 `data/imports/raw/kind/investment-caution-quarterly`에 저장했다. 분기 파일은 총 14,369행, 경계 파일은 9행이다. 각 파일의 요청 구간과 SHA-256을 확인했다.

공공데이터 서비스키는 Git 제외 `.env`에 저장했고 공개 `.env.example`에서는 비웠다. 실제 호출 결과는 다음과 같다.

- 금융위원회 주식시세정보 `getStockPriceInfo`: 2021-08-28~2026-08-28 원본 3,381,406행·339페이지
- 금융위원회 주식발행정보 `getItemBasiInfo_V3`: `NORMAL SERVICE`, 2026-08-28 기준 17,596건
- 정규화 SQLite: 가격 2,866,160행, 보통주 2,620종목, 검증 기간 상장폐지 71종목
- 키움 공식 KODEX 200 벤치마크: 2021-08-30~2026-08-28 1,222거래일
- 완전 구간으로 정규화된 투자주의·경고·위험: 12,628건
- KIND 공식 시장조치 원본: 관리 1,264건, 투자주의환기 550건, 거래정지·재개 4,534건
- 최종 지정 구간: 투자주의·경고·위험·관리·환기·거래정지 합계 16,888건
- schema 2.0 번들: `moneygun-official-20260828.qualified.json.gz`, 2,620종목·2,866,160개 일봉
- 서비스 불변 스냅샷: `snap_95052619574e08fc`, 데이터 자격 6/6 PASS

관리·환기는 지정 및 전체 해제 이벤트를 기간 구간으로 변환했다. 일부해제는 전체 해제로 간주하지 않는다. 거래정지는 공식 시장조치 이력으로 대상 종목을 확인한 뒤 KODEX 200 거래일에 가격 봉이 없는 연속 구간을 정지 기간으로 보수적으로 확정했다. 모든 원본·페이지·기간·체크섬 검사가 통과했으며 불완전 현재 목록은 전체 이력을 덮어쓰지 않는다.

## 자격 관문

가져오기는 아래를 모두 만족해야 성공한다.

- `source`: `KRX_AUTHORIZED_EXPORT`, 공식 공공데이터+KIND(+키움 벤치마크), 또는 `LICENSED_VENDOR`
- `license_basis`: `USER_AUTHORIZED_INTERNAL_RESEARCH` 또는 `OFFICIAL_DATA_CONTRACT`
- 최소 5년 달력 기간과 최소 2종목
- 가격 종목과 생애주기 종목의 정확한 일치
- 검증 기간 중 상장폐지된 보통주 최소 1개와 그 가격 이력
- 과거 지정 상태 최소 1건
- 원본 파일 SHA-256, 출처 URL, 전체 기간 coverage
- 가격·생애주기·지정상태 각 정규화 구간 SHA-256 일치
- 미래 가격, 중복 날짜, 상장 전·상장폐지 후 가격 없음

이 검사를 통과했을 때만 서버가 다음 값을 직접 생성한다. 파일 작성자가 `true`라고 적는 방식은 인정하지 않는다.

```json
{
  "survivorship_bias_controlled": true,
  "historical_designation_states_complete": true
}
```

이 가격·종목상태 번들은 집중투자의 `fundamental_revision_safe` 관문을 열지 않는다. 정정 전 OpenDART 재무 원본의 공시 당시 값을 별도로 검증하는 어댑터가 완성될 때까지 서버는 해당 값을 항상 `false`로 둔다.

## 파일과 API

허용 파일명은 `*.qualified.json` 또는 `*.qualified.json.gz`이며 기본 inbox는 `data/imports`다. 압축 파일 최대 크기는 1GB다.

- `GET /v1/data-qualification/spec`: 전체 규격과 공식 다운로드 링크
- `GET /v1/data-qualification/status`: 운영센터의 6개 데이터 관문
- `GET /v1/data-qualification/daily-refresh/status`: 자동 갱신 진행·최근 성공·실패 상태
- `POST /v1/data-qualification/daily-refresh/run`: 비동기 수집·검증 재시도
- `POST /v1/research/snapshots/qualified-import`: 자격 번들 불변 가져오기
- `POST /v1/research/validations/run`: 집중투자 OOS
- `POST /v1/strategies/close-auction/run`: 종가매매 OOS와 연구 사이클

가져오기 요청 예시:

```json
{
  "file_name": "krx-2021-2026.qualified.json.gz"
}
```

`Idempotency-Key` 헤더도 8자 이상으로 함께 보낸다. 같은 내용은 같은 스냅샷 ID가 되어 중복 저장되지 않는다.

## 시점별 검증 방식

검증 프로토콜 `POINT_IN_TIME_DYNAMIC_UNIVERSE-v3`는 벤치마크 거래일을 기준축으로 사용한다. 각 신호일마다 그날 실제 상장돼 있었고 금지 지정 상태가 아니었던 종목만 후보가 된다. 종목이 상장폐지되거나 가격이 사라져 예정 청산가를 확인할 수 없으면 수익으로 가정하지 않고 해당 거래를 보수적으로 전손 처리한다. 종목별 생애주기·지정상태 인덱스로 같은 판단을 반복 전수검색하지 않으며, 5년 관문은 검증된 달력 구간과 양 끝 거래일의 7일 이내 경계를 함께 확인한다.

기존 공통 거래일 교집합 방식의 결과는 `LEGACY_COMMON_DATES-v1`로 보존한다. 새 결과는 별도 검증·위원회 레코드로 추가되며 과거 보고서를 덮어쓰지 않는다.

## 현재 OOS 결과와 다음 관문

- 집중투자: 1,222거래일·630 OOS일, 재무 시점 안전성 미완료로 거래 0건, `NOT_ELIGIBLE`
- 종가매매: 116거래·순수익 54.1%·Sharpe 0.641·MDD -36.44%, 초과수익·Sharpe·DSR·낙폭·견고성 미달로 `NOT_ELIGIBLE`

가격·종목상태 데이터 작업은 완료됐다. 남은 관문은 정정 전 값까지 고정한 OpenDART 원문 재무 어댑터 또는 재무정보를 쓰지 않는 별도 사전등록 전략, 독립 OOS 성과 통과, R1 그림자 60거래일·100결정, 운영 배포 구성, 릴리스 승인과 소유자의 최종 활성화다.
