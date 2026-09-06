# Signal Guild project guidance

## Product

Signal Guild is a private, single-owner AI investment-company service. The first product is a
KRW 100,000 ring-fenced Korean-stock FOCUS mission. Read `docs/PROJECT_CONTEXT.md`,
`docs/ARCHITECTURE.md`, `docs/MODE_PROFILES.md`, and `docs/HANDOFF.md` before changing
product behavior.

## Commands

- `서비스 기획`: run the plan-service workflow and update durable product docs.
- `서비스 세팅`: audit, repair, and verify the repository and workspace baseline.
- `서비스 설계`: run the design-service workflow and update architecture and visual rules.
- `기능 개발: <기능명>`: implement the named feature against the approved design.
- `릴리스 점검`: run the release quality gate; do not deploy unless explicitly requested.
- `운영 점검`: review live evidence, reliability, cost, security, and priorities.

## Architecture boundaries

- React/TypeScript PWA lives in `apps/web`; Python/FastAPI lives in `apps/api`.
- Keep strategy and risk calculations deterministic. LLM agents may research and explain,
  but may not calculate order quantity, bypass risk, access broker secrets, or submit orders.
- The Execution Guardian is the sole owner of the Kiwoom session and order capability.
- Treat mission capital, mode, mandate, strategy, decision, order, fill, and audit records as
  versioned domain data. Never silently rewrite history.
- Unknown broker responses remain `UNKNOWN` until reconciliation; never blindly retry an order.

## Safety and secrets

- Local prototypes use fictional instruments and `KIWOOM_TRADING_ENABLED=false`.
- Never commit `.env`, tokens, account identifiers, certificates, raw personal data, or broker logs.
- Fail closed for stale data, reconciliation differences, missing evidence, and ambiguous API state.
- Never add leverage, credit, shorting, derivatives, automatic deposits, or averaging down without
  an approved product and risk decision.

## Git and validation

- Preserve unrelated changes and Git history. Do not rewrite or discard user work.
- Use focused validation first: `npm run typecheck`, `npm run build`, and API syntax/import checks.
- Record material behavior or architecture decisions in `docs/DECISIONS.md` and update
  `docs/HANDOFF.md` at the end of substantial work.
- Do not deploy, publish, connect live broker credentials, or activate real orders without explicit
  authorization and a completed release check.
