<div align="center">
  <img src="apps/web/public/brand/moneygun-logo.svg" width="120" alt="MoneyGun 로고" />

  # MoneyGun

  **내가 직접 쓰려고 만든 작은 AI 투자회사**

  [![CI](https://github.com/hoya0328/MoneyGun/actions/workflows/ci.yml/badge.svg)](https://github.com/hoya0328/MoneyGun/actions/workflows/ci.yml)
  ![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=white)
  ![FastAPI](https://img.shields.io/badge/FastAPI-Python_3.13-009688?logo=fastapi&logoColor=white)
  ![Tests](https://img.shields.io/badge/API_tests-102_passed-45E6A6)
</div>

---

## 왜 만들었나

주식 자동매매를 만들면서 단순히 신호가 뜨면 바로 주문하는 봇은 쓰고 싶지 않았습니다.

뉴스를 보는 직원, 차트를 보는 직원, 매수에 찬성하는 직원과 반대하는 직원이 각자 의견을 내고 마지막에 위험 담당이 한 번 더 막아주는 구조라면 판단 과정을 내가 이해하기도 쉽고 나중에 규칙을 고치기도 편할 것 같았습니다.

그래서 자동매매 프로그램을 하나의 작은 회사처럼 만들고, 복잡한 상태는 픽셀 직원들이 일하는 화면으로 볼 수 있게 구성했습니다.

<p align="center">
  <img src="apps/web/public/assets/moneygun-pixel-office-v1.png" width="900" alt="MoneyGun 직원 사무실" />
</p>

## 직원들

| 직원 | 하는 일 |
|---|---|
| 김데이터 | 시세, 종목, 거래정지와 주의종목 데이터 확인 |
| 김뉴스 | 공시와 뉴스를 확인하고 위험한 매수 차단 |
| 김산업 | 종목이 속한 산업과 관련 흐름 정리 |
| 김차트 | 추세, 거래량, 변동성 같은 차트 신호 계산 |
| 김찬성 | 지금 사도 되는 이유 정리 |
| 김반대 | 놓친 위험이나 사면 안 되는 이유 정리 |
| 김안전 | 손실 한도와 데이터 상태를 검사하고 필요하면 거부 |
| 김투자 | 모든 의견을 모아 매수, 보유, 매도 제안 |
| 김주문 | 최종 승인된 주문만 증권사로 전달하고 체결 확인 |

AI는 자료를 읽고 설명하는 역할까지만 맡습니다. 주문 수량과 손실 한도, 실제 주문 가능 여부는 정해진 코드가 계산합니다.

## 현재 들어간 매매 모드

- **집중투자** — 조건이 좋은 소수 종목에 집중
- **종가매매** — 장 마감 전 신호를 확인하고 종가 구간 활용
- **장초단타** — 장 초반 흐름을 보고 짧게 매수·매도
- **안전투자** — 유동성과 변동성 기준을 더 보수적으로 적용
- **장기투자** — 잦은 매매보다 긴 추세를 기준으로 운용

모드가 달라도 일일 손실선, 동시 보유 한도, 위험 공시 차단, 기존 보유종목 제외와 긴급 정지 규칙은 공통으로 적용됩니다.

## 안전장치

- 기본 상태에서는 실제 주문 비활성화
- 신용, 미수, 공매도, 파생상품 사용 안 함
- 시세가 오래됐거나 계좌 정보가 맞지 않으면 주문 중단
- 증권사 응답이 애매한 주문은 자동으로 다시 넣지 않음
- 전략 판단부터 주문과 체결까지 기록을 남겨 나중에 확인 가능
- API 키와 계좌 정보는 저장소에 올리지 않음

> 이 프로젝트는 개인용 투자 자동화 실험입니다. 수익을 보장하지 않으며 원금 전액 손실 가능성이 있습니다.

## 기술 구성

```text
공식 데이터·공시·시세
        ↓
뉴스·산업·차트 분석
        ↓
김찬성 ↔ 김반대
        ↓
김안전 → 김투자 → 김주문
        ↓
Kiwoom REST API + 거래 기록
```

- 화면: React, TypeScript
- API와 전략: FastAPI, Python
- 로컬 기록: SQLite
- 실행 환경: Windows 로컬 PC

## 로컬 실행

Node.js 24 이상과 Python 3.13 이상이 필요합니다.

```powershell
git clone https://github.com/hoya0328/MoneyGun.git
Set-Location MoneyGun
npm install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "apps/api[dev]"
Copy-Item .env.example .env
.\scripts\start-local.ps1
```

실제 키와 계좌 정보는 저장소가 아닌 로컬 보안 저장소에 따로 넣어야 합니다.

## 현재 상태

한국 주식 기준으로 공식 데이터 갱신, 공시 감시, 다섯 가지 운용 모드, 주문 전 안전검사와 재시작 후 복구까지 구현했습니다. 지금은 실제 운용 기록을 쌓으면서 전략 규칙을 계속 수정하는 단계입니다.

자세한 사용 방법은 [사용 가이드](docs/USER_GUIDE.md), 전체 진행 상황은 [로드맵](docs/ROADMAP.md)에서 볼 수 있습니다.

---

<div align="center">
  <strong>판단 과정은 보이게, 실제 주문은 신중하게.</strong>
</div>
