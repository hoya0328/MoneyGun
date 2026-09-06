# Contributing to Signal Guild

Signal Guild는 실제 금융 계좌와 연결될 수 있는 프로젝트이므로 일반적인 UI 프로젝트보다 변경 경계를
엄격하게 다룹니다. 작은 변경으로 시작하고, 주문·수량·위험 계산은 항상 결정론적으로 유지해 주세요.

## 개발 환경

- Node.js 24+
- Python 3.13+
- Windows PowerShell은 데스크톱 운영 스크립트에만 필요

```powershell
npm install
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "apps/api[dev]"
```

## 변경 원칙

1. LLM은 조사와 설명만 담당하며 주문 수량 계산·위험 우회·브로커 제출을 맡지 않습니다.
2. Execution Guardian만 주문 세션을 소유합니다.
3. 불명확한 브로커 응답은 `UNKNOWN`으로 남기고 자동 재시도하지 않습니다.
4. 실제 자격증명, 계좌 식별자, 보유종목, 주문·체결 로그와 유료 데이터는 커밋하지 않습니다.
5. 전략·위험·결정·주문·체결·감사 기록은 과거를 덮어쓰지 않습니다.

## 검증

```powershell
.\.venv\Scripts\python.exe -m ruff check apps/api/src apps/api/tests
.\.venv\Scripts\python.exe -m pytest apps/api/tests -q
npm run typecheck
npm run build
```

Pull request에는 변경 이유, 검증 결과, 실패 폐쇄 영향과 롤백 방법을 적어 주세요.
