import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import {
  activateKillSwitch,
  acknowledgeNewsEvent,
  acknowledgeOperationsNotification,
  approveLiveIntent,
  captureOpeningRangeRealtime,
  cancelLiveIntent,
  createLiveIntent,
  createOperationsBackup,
  createOfficialShadowOrder,
  createShadowOrder,
  fetchExecutionStatus,
  fetchActivationReadiness,
  fetchDataQualificationStatus,
  fetchLatestCommitteeProgram,
  fetchLatestCloseAuction,
  fetchLiveCloseAuctionStatus,
  fetchL0ModesStatus,
  fetchOpeningRangeLiveStatus,
  fetchL0ExitStatus,
  fetchLatestOpeningRange,
  fetchLatestResearchCycle,
  fetchLatestResearchValidation,
  fetchNewsStatus,
  fetchKiwoomStatus,
  fetchPrimaryMission,
  fetchLiveIntents,
  fetchOperationsNotifications,
  fetchOperationsReadiness,
  fetchAutonomyReadiness,
  fetchPerformanceReport,
  fetchPilotCandidates,
  fetchPilotStatus,
  fetchRuntimeMonitor,
  fetchStrategyComparison,
  fetchDeploymentReadiness,
  fetchShadowOrders,
  runAccountReconciliation,
  runDailyOperations,
  runCommitteeReplay,
  runCloseAuction,
  runCloseAuctionPilotTick,
  runL0ModeTick,
  runOpeningRangePilotTick,
  runL0ExitTick,
  runOpeningRangeReplay,
  runResearchCycle,
  runResearchValidation,
  runOperationsFailureDrills,
  runOperationsRecoveryDrill,
  pollNews,
  recoverDesktopLive,
  importQualifiedSnapshot,
  importOpeningRangeArchive,
  generatePilotReviews,
  resumeKillSwitch,
  setExecutionAutomation,
  settleOfficialShadowOrder,
  simulateShadowOrder,
  submitLiveIntent,
  syncKiwoomAccount,
  syncOfficialQuote,
  transitionExecutionStage,
  triggerDailyMarketRefresh,
  type KiwoomStatus,
  type CommitteeProgram,
  type CloseAuctionBundle,
  type LiveCloseAuctionStatus,
  type L0ModesStatus,
  type OpeningRangeLiveStatus,
  type L0ExitStatus,
  type OpeningRangeBundle,
  type OpeningRangeCaptureResult,
  type MissionSummary,
  type ResearchCycle,
  type ResearchValidation,
  type ShadowOrder,
  type ExecutionStatus,
  type ActivationPortfolio,
  type LiveOrderIntent,
  type OperationsNotification,
  type OperationsReadiness,
  type DataQualificationStatus,
  type AutonomyReadiness,
  type PerformanceReport,
  type PilotStatus,
  type RuntimeMonitor,
  type StrategyComparison,
  type DeploymentReadiness,
  type NewsStatus,
} from './api'
import { agents, candidates, evidence, ladder, type Agent, type AgentState } from './mockData'

type Page = 'cockpit' | 'committee' | 'agents' | 'orders' | 'audit'

const navItems: Array<{ id: Page; label: string; short: string }> = [
  { id: 'cockpit', label: '오늘의 작전실', short: '작전실' },
  { id: 'committee', label: '투자위원회', short: '위원회' },
  { id: 'agents', label: '직원 사무실·도감', short: '직원들' },
  { id: 'orders', label: '그림자 주문', short: '그림자주문' },
  { id: 'audit', label: '운영 센터', short: '운영' },
]

const officeHotspots: Record<string, { x: number; y: number }> = {
  DATA: { x: 8, y: 48 },
  NOVA: { x: 15, y: 24 },
  SERENITY: { x: 43, y: 25 },
  PULSE: { x: 72, y: 23 },
  BULL: { x: 36, y: 52 },
  BEAR: { x: 57, y: 52 },
  RISK: { x: 79, y: 54 },
  ACE: { x: 20, y: 78 },
  OPS: { x: 76, y: 82 },
}

const stateLabel: Record<AgentState, string> = {
  DONE: '완료',
  WORKING: '분석 중',
  WAITING: '대기',
  CONFLICT: '이견',
  READY: '준비',
}

const fallbackMission: MissionSummary = {
  id: 'mission_focus_001',
  name: '10만 원 집중투자 미션',
  mode_code: 'FOCUS',
  mode_version: 1,
  mode_display_name: '집중투자',
  stage: 'R0',
  state: 'ACTIVE',
  seed_capital_krw: 100_000,
  goal_capital_krw: 10_000_000,
  available_krw: 100_000,
  exposed_krw: 0,
  reserved_profit_krw: 0,
  equity_krw: 100_000,
  halt_equity_krw: 70_000,
  trading_enabled: false,
  created_at: '2026-08-31T00:00:00+00:00',
}

const formatKrw = (value: number) => `₩${value.toLocaleString('ko-KR')}`

function StatusPill({ state }: { state: AgentState }) {
  return <span className={`status-pill status-${state.toLowerCase()}`}>{stateLabel[state]}</span>
}

function AgentAvatar({ agent }: { agent: Agent }) {
  return <div className={`agent-avatar pixel-avatar accent-${agent.accent}`} aria-hidden="true"><span>{agent.icon}</span></div>
}

function Header({ halted, onKill, stage, pending, title }: {
  halted: boolean
  onKill: () => void
  stage: string
  pending: boolean
  title: string
}) {
  return (
    <header className="topbar">
      <div className="topbar-title">
        <span className="eyebrow">머니건 투자회사 · 1일차</span>
        <strong>{title}</strong>
      </div>
      <div className="system-signals" aria-label="시스템 상태">
        <span className="system-state"><i className="dot dot-good" />KRX 종료</span>
        <span className="system-state"><i className="dot dot-good" />데이터 정상</span>
        <span className="system-state"><i className="dot dot-warning" />실행 단계 {stage}</span>
        <button className={`kill-button ${halted ? 'is-active' : ''}`} onClick={onKill} disabled={pending}>
          {pending ? '처리 중…' : halted ? '중지됨 · 재개' : '긴급 중지'}
        </button>
      </div>
    </header>
  )
}

function Sidebar({ page, setPage, stage, automation }: {
  page: Page
  setPage: (page: Page) => void
  stage: string
  automation: boolean
}) {
  return (
    <aside className="sidebar">
      <button className="brand" onClick={() => setPage('cockpit')} aria-label="MoneyGun 홈">
        <img className="brand-mark" src="/brand/moneygun-logo.svg" alt="" aria-hidden="true" />
        <span><strong>MoneyGun</strong><small>픽셀 투자회사</small></span>
      </button>
      <nav className="desktop-nav" aria-label="주요 메뉴">
        {navItems.map((item, index) => (
          <button
            key={item.id}
            className={page === item.id ? 'nav-item active' : 'nav-item'}
            onClick={() => setPage(item.id)}
          >
            <span className="nav-index">0{index + 1}</span>{item.label}
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <span className="mode-badge">{stage === 'L0' ? 'L0 파일럿 v1.0' : '집중투자 v0.1'}</span>
        <p>{stage === 'L0' ? `5만 원 · ${automation ? '무인 실주문' : '승인형 주문'}` : '1일차 · 그림자 운영'}</p>
        <p>{stage === 'L0' ? (automation ? '후보·공시·주문 자동화 실행 중입니다.' : '실거래 잠금 상태를 먼저 확인하세요.') : '직원들은 연구와 그림자 검증 중입니다.'}</p>
      </div>
    </aside>
  )
}

function MobileNav({ page, setPage }: { page: Page; setPage: (page: Page) => void }) {
  return (
    <nav className="mobile-nav" aria-label="모바일 메뉴">
      {navItems.map((item) => (
        <button
          key={item.id}
          className={page === item.id ? 'active' : ''}
          onClick={() => setPage(item.id)}
        >
          <span className="mobile-nav-dot" />{item.short}
        </button>
      ))}
    </nav>
  )
}

function MetricCard({ label, value, meta, tone = 'plain' }: {
  label: string
  value: string
  meta: string
  tone?: 'plain' | 'focus' | 'risk'
}) {
  return (
    <article className={`metric-card tone-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{meta}</small>
    </article>
  )
}

function MissionChart() {
  return (
    <div className="chart-wrap" aria-label="샘플 미션 순자산 차트">
      <svg viewBox="0 0 720 190" role="img" aria-labelledby="chart-title">
        <title id="chart-title">프로토타입 미션 순자산 추이</title>
        <defs>
          <linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#d7ff4f" stopOpacity="0.25" />
            <stop offset="100%" stopColor="#d7ff4f" stopOpacity="0" />
          </linearGradient>
        </defs>
        <g className="chart-grid">
          <line x1="0" y1="28" x2="720" y2="28" />
          <line x1="0" y1="85" x2="720" y2="85" />
          <line x1="0" y1="142" x2="720" y2="142" />
        </g>
        <path
          className="chart-area"
          d="M0 136 C45 132 62 142 102 126 S170 108 210 116 S278 90 314 96 S370 70 412 82 S474 61 520 65 S580 38 620 49 S682 24 720 31 L720 190 L0 190 Z"
        />
        <path
          className="chart-line"
          d="M0 136 C45 132 62 142 102 126 S170 108 210 116 S278 90 314 96 S370 70 412 82 S474 61 520 65 S580 38 620 49 S682 24 720 31"
        />
        <circle cx="720" cy="31" r="5" className="chart-point" />
      </svg>
      <div className="chart-axis"><span>D-30</span><span>D-20</span><span>D-10</span><span>오늘</span></div>
    </div>
  )
}

function GoalLadder() {
  return (
    <section className="goal-ladder" aria-label="100배 목표 사다리">
      {ladder.map((step, index) => (
        <div className={step.reached ? 'goal-step reached' : 'goal-step'} key={step.label}>
          <span className="goal-node">{step.reached ? '✓' : index + 1}</span>
          <strong>{step.label}</strong>
          <small>{step.multiple}</small>
        </div>
      ))}
    </section>
  )
}

function AgentFlow({ cycle }: { cycle?: ResearchCycle | null }) {
  const liveAgents = agents.map((agent) => {
    const report = cycle?.reports.find((item) => item.code === agent.code)
    return report ? { ...agent, state: report.state, summary: report.summary } : agent
  })
  const flowAgents = liveAgents.filter((agent) =>
    ['NOVA', 'SERENITY', 'PULSE', 'BULL', 'BEAR', 'RISK', 'ACE', 'OPS'].includes(agent.code),
  )
  return (
    <div className="agent-flow">
      {flowAgents.map((agent, index) => (
        <div className="flow-fragment" key={agent.code}>
          <div className={`flow-agent accent-${agent.accent}`}>
            <span>{agent.name}</span><StatusPill state={agent.state} />
          </div>
          {index < flowAgents.length - 1 && <span className="flow-arrow" aria-hidden="true">→</span>}
        </div>
      ))}
    </div>
  )
}

function OfficeWorld({
  compact = false,
  onOpenOffice,
  cycle,
}: {
  compact?: boolean
  onOpenOffice?: () => void
  cycle?: ResearchCycle | null
}) {
  const [selectedCode, setSelectedCode] = useState(compact ? 'SERENITY' : 'NOVA')
  const officeAgents = agents.map((agent) => {
    const report = cycle?.reports.find((item) => item.code === agent.code)
    return report ? { ...agent, state: report.state, summary: report.summary, speech: report.summary } : agent
  })
  const selectedAgent = officeAgents.find((agent) => agent.code === selectedCode) ?? officeAgents[0]
  const completed = officeAgents.filter((agent) => agent.state === 'DONE').length

  return (
    <section className={`pixel-office panel ${compact ? 'is-compact' : ''}`} aria-label="MoneyGun 픽셀 직원 사무실">
      <div className="office-toolbar">
        <div>
          <span className="pixel-kicker">DAY 001 · AFTER MARKET</span>
          <h2>{compact ? '직원들이 오늘의 투자 퀘스트를 수행 중이에요' : 'MoneyGun 픽셀 오피스'}</h2>
        </div>
        <div className="office-day-status" aria-label={`직원 업무 ${completed}명 완료`}>
          <span><i className="dot dot-good" />{completed}/9 업무 완료</span>
          <div className="day-meter"><i style={{ width: `${(completed / agents.length) * 100}%` }} /></div>
        </div>
      </div>

      <div className="office-stage">
        <img
          className="office-art"
          src="/assets/moneygun-pixel-office-v1.png"
          alt="뉴스, 공급망, 차트, 토론, 위험, 포트폴리오, 주문 구역에서 직원들이 협업하는 픽셀 오피스"
        />
        <div className="office-shade" aria-hidden="true" />
        {officeAgents.map((agent) => {
          const position = officeHotspots[agent.code]
          const style = { '--hotspot-x': `${position.x}%`, '--hotspot-y': `${position.y}%` } as CSSProperties
          return (
            <button
              className={`agent-hotspot accent-${agent.accent} ${selectedAgent.code === agent.code ? 'is-selected' : ''}`}
              key={agent.code}
              style={style}
              onClick={() => setSelectedCode(agent.code)}
              aria-pressed={selectedAgent.code === agent.code}
              aria-label={`${agent.name}, ${agent.role}, ${stateLabel[agent.state]}`}
            >
              <span className="hotspot-beacon" aria-hidden="true" />
              <span className="hotspot-label"><b>{agent.icon} {agent.name}</b><small>{agent.role} · {stateLabel[agent.state]}</small></span>
            </button>
          )
        })}

        <article className={`office-dialog accent-${selectedAgent.accent}`} aria-live="polite">
          <AgentAvatar agent={selectedAgent} />
          <div className="office-dialog-copy">
            <div><strong>{selectedAgent.name}</strong><span>{selectedAgent.nickname} · {selectedAgent.room}</span></div>
            <p>“{selectedAgent.speech}”</p>
          </div>
          <StatusPill state={selectedAgent.state} />
        </article>
      </div>

      <div className="office-questline" aria-label="오늘의 협업 진행 순서">
        {[
          ['01', '자료 모으기', 'DONE'],
          ['02', '세 갈래 분석', 'WORKING'],
          ['03', '황소·곰 토론', 'CONFLICT'],
          ['04', '위험 문지기', 'WAITING'],
          ['05', '결정·주문', 'WAITING'],
        ].map(([number, label, state], index) => (
          <div className={`quest-step quest-${state.toLowerCase()}`} key={number}>
            <span>{number}</span><b>{label}</b>{index < 4 && <i aria-hidden="true">›</i>}
          </div>
        ))}
      </div>

      {compact && onOpenOffice && <button className="office-open-button" onClick={onOpenOffice}>사무실 전체 보기 <span>→</span></button>}
    </section>
  )
}

function ResearchCyclePanel({
  cycle,
  status,
  onRun,
}: {
  cycle: ResearchCycle | null
  status: 'loading' | 'idle' | 'running' | 'error'
  onRun: () => void
}) {
  const best = cycle?.candidates[0]
  return (
    <section className="research-console panel" aria-label="P0 연구 사이클">
      <div className="research-console-copy">
        <span className="pixel-kicker">P0 RESEARCH ENGINE · {cycle?.source ?? 'READY'}</span>
        <h2>{cycle ? `${cycle.strategy_id} 업무 완료` : '오늘의 직원 업무를 시작할 준비가 됐어요'}</h2>
        <p>
          {cycle
            ? `${best?.name} ${best?.score}점 · 김안전 ${cycle.risk.result} · 김투자 ${cycle.decision.action}`
            : '고정 스냅샷으로 데이터 검사부터 최종 결정까지 한 번에 실행합니다.'}
        </p>
      </div>
      {cycle && (
        <div className="research-facts">
          <span>스냅샷 <b>{cycle.snapshot_id.slice(-6)}</b></span>
          <span>백테스트 <b>{cycle.backtest.trade_count}회</b></span>
          <span>주문 <b className="negative">차단</b></span>
        </div>
      )}
      <button className="primary-button research-run" onClick={onRun} disabled={status === 'running'}>
        {status === 'running' ? '직원들이 일하는 중…' : cycle ? '같은 스냅샷 재실행' : '오늘 업무 시작'}
      </button>
      {status === 'error' && <p className="research-error" role="alert">사이클 실행에 실패했습니다. API 상태를 확인한 뒤 다시 시도하세요.</p>}
    </section>
  )
}

const gateLabels: Record<string, string> = {
  snapshot_quality: '데이터 품질',
  source_is_real_and_authorized: '승인된 실데이터',
  minimum_five_year_history: '5년 이력',
  minimum_two_year_oos: 'OOS 2년',
  minimum_100_trades: '100거래',
  positive_net_excess_return: '비용 후 초과수익',
  sharpe_at_least_0_8: 'Sharpe 0.8',
  deflated_sharpe_95pct: 'DSR 95%',
  drawdown_within_focus_limit: 'MDD -30% 이내',
  fundamental_revision_safe: '과거 정정 전 재무값 고정',
  survivorship_bias_controlled: '상장폐지 포함 생존편향 제거',
  historical_designation_states_complete: '과거 투자주의·거래정지 상태',
  drawdown_within_close_limit: 'MDD -15% 이내',
  signal_strictly_precedes_entry: '신호일이 진입일보다 앞섬',
  parameter_robustness_plus_minus_20pct: '파라미터 ±20% 견고성',
  point_in_time_two_year_intraday_history: '시점고정 분봉 2년',
  final_126_sessions_untouched: '최종 OOS 126일 미사용',
  minimum_300_oos_trades: 'OOS 완결 300거래',
  expectancy_at_least_0_15r: '순기대값 +0.15R',
  sharpe_at_least_1_2: 'Sharpe 1.2',
  profit_factor_at_least_1_2: 'Profit Factor 1.2',
  max_drawdown_within_8pct: 'MDD -8% 이내',
  worst_month_within_4pct: '최악 월 -4% 이내',
  double_slippage_expectancy_positive: '슬리피지 2배 기대값 양수',
  zero_broker_orders_during_research: '연구 중 브로커 주문 0건',
  all_positions_flat_by_1100: '11시 전 포지션 0',
  shadow_60_days_300_signals: '그림자 60일·300신호',
  authorized_non_fixture_source: '승인된 실시간 원본',
}

const openingExitLabels: Record<string, string> = {
  STOP_LOSS: '최초 손절',
  TIME_STOP: '10분 진전 없음',
  VWAP_EXIT: 'VWAP 2봉 이탈',
  FORCE_FLAT: '11시 강제 청산',
  RUNNER_TRAILING_STOP: '고점 추적 청산',
  RUNNER_STAGNATION_EXIT: '7분 신고가 정체',
  DAILY_HARD_PROFIT_LOCK: '당일 +15% 수익 잠금',
}

function ValidationPanel({
  validation,
  status,
  disabled,
  onRun,
}: {
  validation: ResearchValidation | null
  status: 'loading' | 'idle' | 'running' | 'error'
  disabled: boolean
  onRun: () => void
}) {
  const passed = validation ? Object.values(validation.gates).filter(Boolean).length : 0
  return (
    <section className="validation-console panel" aria-label="워크포워드 OOS 검증">
      <div className="validation-copy">
        <span className="pixel-kicker">R0 → R1 GATE · PURGED WALK-FORWARD</span>
        <h2>{validation ? (validation.promotion_eligible ? 'R1 검토 가능' : '아직 승격 불가') : 'OOS 검증을 실행하세요'}</h2>
        <p>
          {validation
            ? `${passed}/${Object.keys(validation.gates).length} 게이트 통과 · ${validation.history.oos_days} OOS일 · 비용 후 ${validation.metrics.net_return_pct}%`
            : '학습 구간과 시험 구간 사이를 비우고, 신호 다음 거래일 가격과 비용을 적용합니다.'}
        </p>
      </div>
      {validation && (
        <div className="validation-metrics">
          <span>거래 <b>{validation.metrics.trade_count}</b></span>
          <span>Sharpe <b>{validation.metrics.sharpe}</b></span>
          <span>MDD <b>{validation.metrics.max_drawdown_pct}%</b></span>
          <span>승격 <b className={validation.promotion_eligible ? 'positive' : 'negative'}>{validation.promotion_eligible ? '검토 가능' : '차단'}</b></span>
        </div>
      )}
      {validation && validation.failed_gates.length > 0 && (
        <div className="gate-list" aria-label="미통과 게이트">
          {validation.failed_gates.map((gate) => <span key={gate}>{gateLabels[gate] ?? gate}</span>)}
        </div>
      )}
      <button className="secondary-button validation-run" onClick={onRun} disabled={disabled || status === 'running'}>
        {status === 'running' ? '검증 계산 중…' : validation ? 'OOS 다시 검증' : 'OOS 검증 실행'}
      </button>
      {status === 'error' && <p className="research-error" role="alert">OOS 검증에 실패했습니다. 먼저 연구 사이클을 실행하세요.</p>}
    </section>
  )
}

function CommitteeProgramPanel({
  program,
  status,
  onRun,
}: {
  program: CommitteeProgram | null
  status: 'loading' | 'idle' | 'running' | 'error'
  onRun: () => void
}) {
  const summary = program?.summary
  return (
    <section className="committee-program panel" aria-label="20거래일 자동 위원회">
      <div className="program-heading">
        <div>
          <span className="pixel-kicker">P3 AUTO COMMITTEE · REPLAY ONLY</span>
          <h2>{summary?.readiness.replay_complete ? '20일 위원회 리플레이 완료' : '20거래일 자동 위원회 준비'}</h2>
          <p>{summary?.readiness.reason ?? '날짜별 9명 보고서와 결정을 자동 생성하고 직원 판단을 채점합니다.'}</p>
        </div>
        <button className="primary-button program-run" onClick={onRun} disabled={status === 'running'}>
          {status === 'running' ? '20일 업무 처리 중…' : program ? '같은 20일 재검증' : '20일 리플레이 시작'}
        </button>
      </div>
      {program && summary && (
        <>
          <div className="program-facts">
            <span>패키지 <b>{summary.completed_days}/{summary.target_days}</b></span>
            <span>채점 완료 <b>{summary.evaluated_days}일</b></span>
            <span>결과 대기 <b>{summary.pending_outcome_days}일</b></span>
            <span>실제 운영 <b className="negative">{summary.readiness.real_operating_days}일</b></span>
          </div>
          <div className="program-days" aria-label="날짜별 위원회 생성 상태">
            {program.days.map((day) => (
              <span
                key={day.trade_date}
                className={`program-day is-${day.state.toLowerCase()}`}
                title={`${day.trade_date} · ${day.state} · ${day.attempts}회 시도`}
                aria-label={`${day.trade_date} ${day.state}`}
              />
            ))}
          </div>
          <div className="agent-scoreboard">
            {summary.agent_scorecards.map((card) => (
              <article key={card.code}>
                <div><strong>{card.name}</strong><b>{card.overall_score}</b></div>
                <span>정확 {card.accuracy_pct}% · 근거 {card.evidence_quality_pct}%</span>
                <small>과신 {card.overconfidence_count}회 · {card.scored_days}일 채점</small>
              </article>
            ))}
          </div>
          <div className="replay-warning"><strong>REPLAY</strong><span>자동화 배관 검증입니다. 실제 20거래일 게이트와 실거래 권한은 0일입니다.</span></div>
        </>
      )}
      {status === 'error' && <p className="research-error" role="alert">20일 위원회 실행에 실패했습니다. 실패 날짜는 다음 실행에서만 재시도됩니다.</p>}
    </section>
  )
}

function CandidateTable({ onInspect, cycle }: { onInspect: () => void; cycle?: ResearchCycle | null }) {
  const rows = cycle
    ? cycle.candidates.map((candidate) => ({
        ...candidate,
        risk: candidate.rank === 1 ? cycle.risk.result : '검토 완료',
        change: `${candidate.return_63_pct >= 0 ? '+' : ''}${candidate.return_63_pct}%`,
      }))
    : candidates.map((candidate) => ({
        ...candidate,
        sector: '샘플',
        liquidity_risk: 0,
        market_regime: 0,
      }))
  return (
    <div className="table-scroll">
      <table className="candidate-table">
        <thead>
          <tr><th>순위</th><th>후보</th><th>총점</th><th>모멘텀</th><th>품질</th><th>유동성</th><th>시장</th><th>촉매</th><th>위험</th><th>63일</th></tr>
        </thead>
        <tbody>
          {rows.map((candidate) => (
            <tr key={candidate.symbol} onClick={onInspect} tabIndex={0}>
              <td><span className="rank">{candidate.rank}</span></td>
              <td><strong>{candidate.name}</strong><small>{candidate.symbol} · {candidate.sector}</small></td>
              <td><b className="score">{candidate.score}</b></td>
              <td>{candidate.momentum}/40</td>
              <td>{candidate.quality}/25</td>
              <td>{candidate.liquidity_risk}/15</td>
              <td>{candidate.market_regime}/10</td>
              <td>{candidate.catalyst}/10</td>
              <td><span className={`risk-text ${candidate.risk === '주의' ? 'warning' : ''}`}>{candidate.risk}</span></td>
              <td className={candidate.change.startsWith('+') ? 'positive' : 'negative'}>{candidate.change}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Cockpit({
  onInspect,
  onPage,
  mission,
  connection,
  onRetry,
  cycle,
  cycleStatus,
  onRunCycle,
  validation,
  validationStatus,
  onRunValidation,
  program,
  programStatus,
  onRunProgram,
}: {
  onInspect: () => void
  onPage: (page: Page) => void
  mission: MissionSummary
  connection: 'loading' | 'connected' | 'fallback'
  onRetry: () => void
  cycle: ResearchCycle | null
  cycleStatus: 'loading' | 'idle' | 'running' | 'error'
  onRunCycle: () => void
  validation: ResearchValidation | null
  validationStatus: 'loading' | 'idle' | 'running' | 'error'
  onRunValidation: () => void
  program: CommitteeProgram | null
  programStatus: 'loading' | 'idle' | 'running' | 'error'
  onRunProgram: () => void
}) {
  return (
    <>
      <section className="page-heading">
        <div><span className="eyebrow">{mission.id} · {mission.stage}</span><h1>오늘의 픽셀 투자회사</h1></div>
        <button className={`api-connection is-${connection}`} onClick={onRetry}>
          <i className={`dot ${connection === 'connected' ? 'dot-good' : 'dot-warning'}`} />
          {connection === 'connected' ? '원장 DB 연결됨' : connection === 'loading' ? '원장 연결 중' : '샘플값 · 다시 연결'}
        </button>
      </section>

      <OfficeWorld compact onOpenOffice={() => onPage('agents')} cycle={cycle} />
      <ResearchCyclePanel cycle={cycle} status={cycleStatus} onRun={onRunCycle} />
      <ValidationPanel validation={validation} status={validationStatus} disabled={!cycle} onRun={onRunValidation} />
      <CommitteeProgramPanel program={program} status={programStatus} onRun={onRunProgram} />

      <section className="metrics-grid">
        <MetricCard label="미션 순자산" value={formatKrw(mission.equity_krw)} meta={`${mission.mode_display_name} v${mission.mode_version} · 복식 원장`} tone="focus" />
        <MetricCard label="사용 가능 현금" value={formatKrw(mission.available_krw)} meta="원장 직접 수정 금지" />
        <MetricCard label="현재 노출" value={formatKrw(mission.exposed_krw)} meta="L1 상한 ₩50,000" />
        <MetricCard label="열린 계획위험" value="₩0" meta="모드 상한 ₩15,000" />
        <MetricCard label="이익 금고" value={formatKrw(mission.reserved_profit_krw)} meta="자동 주문 사용 불가" />
        <MetricCard label="미션 중지선" value={formatKrw(mission.halt_equity_krw)} meta="고점 대비 -30%" tone="risk" />
      </section>

      <section className="dashboard-grid">
        <article className="panel mission-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">MISSION EQUITY</span><h2>100배 도전 진행률</h2></div>
            <div className="mission-return"><strong>1.00×</strong><small>목표 100×</small></div>
          </div>
          <MissionChart />
          <GoalLadder />
          <div className="loss-disclosure"><strong>최대 손실 가능</strong><span>격리된 미션 자본 전액 ₩100,000</span></div>
        </article>

        <article className="panel status-panel">
          <div className="panel-heading"><div><span className="eyebrow">SYSTEM</span><h2>주문 가능 상태</h2></div><span className="large-status">SHADOW</span></div>
          <div className="status-list">
            <div><span>시장 데이터</span><strong className="positive">정상 · 15:42</strong></div>
            <div><span>투자위원회</span><strong className={cycle ? 'positive' : 'warning'}>{cycle ? `${cycle.reports.length}/9 완료` : '실행 대기'}</strong></div>
            <div><span>위험 엔진</span><strong className={cycle ? 'positive' : ''}>{cycle?.risk.result ?? '판정 대기'}</strong></div>
            <div><span>키움 연결</span><strong className={cycle?.source === 'KIWOOM_OFFICIAL_REST' ? 'positive' : 'warning'}>{cycle?.source === 'KIWOOM_OFFICIAL_REST' ? '공식 조회 전용' : '실거래 차단'}</strong></div>
            <div><span>잔고 대사</span><strong className="positive">차이 ₩0</strong></div>
          </div>
          <button className="secondary-button full" onClick={() => onPage('audit')}>감사 상태 확인</button>
        </article>
      </section>

      <section className="panel wide-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">{cycle?.strategy_id ?? 'FOCUS SCANNER · 실행 전'}</span><h2>오늘의 집중투자 후보</h2></div>
          <span className="timestamp">{cycle ? `스냅샷 ${cycle.as_of.slice(0, 16)}` : '업무 사이클을 실행하세요'}</span>
        </div>
        <CandidateTable onInspect={onInspect} cycle={cycle} />
      </section>
    </>
  )
}

function Committee({ onDecision, cycle }: { onDecision: (message: string) => void; cycle: ResearchCycle | null }) {
  const candidate = cycle?.candidates[0]
  const bull = cycle?.reports.find((report) => report.code === 'BULL')
  const bear = cycle?.reports.find((report) => report.code === 'BEAR')
  const liveEvidence = cycle?.evidence ?? evidence.map((item, index) => ({
    id: `mock-${index}`,
    source: item.source,
    title: item.title,
    published_at: item.time,
    verified: item.verified,
  }))
  const bullClaims = bull?.claims.map((claim) => claim.text) ?? [
    '20일 고점 돌파와 상대 모멘텀 상위권',
    'DART 샘플 공급계약으로 수주 가시성 증가',
  ]
  const bearClaims = bear?.claims.map((claim) => claim.text) ?? [
    '상위 고객 의존도가 높아 계약 지연에 취약',
    '갭 발생 시 계획 손실이 확대될 수 있음',
  ]
  return (
    <>
      <section className="page-heading"><div><span className="eyebrow">COMMITTEE CYCLE · {cycle?.id ?? 'NOT RUN'}</span><h1>투자위원회</h1></div><span className="large-status">{cycle ? 'COMPLETE' : 'WAITING'}</span></section>
      <section className="committee-hero panel">
        <div className="candidate-title"><span className="rank">1</span><div><span className="eyebrow">{candidate?.symbol ?? '사이클 실행 전'} · RESEARCH ONLY</span><h2>{candidate?.name ?? '오늘 업무를 먼저 실행하세요'}</h2></div></div>
        <div className="score-block"><strong>{candidate?.score ?? '—'}</strong><span>/ 100</span><small>결정론적 후보 점수</small></div>
      </section>
      <AgentFlow cycle={cycle} />
      <section className="debate-grid">
        <article className="panel debate-card bull-card">
          <div className="debate-title"><span>찬성</span><h2>김찬성의 상승 논리</h2></div>
          <ol>{bullClaims.map((claim) => <li key={claim}>{claim}</li>)}</ol>
          <p className="confidence">주장 신뢰도 {bull?.confidence ?? 0.72} · 포지션 크기에 사용하지 않음</p>
        </article>
        <article className="panel debate-card bear-card">
          <div className="debate-title"><span>반대</span><h2>김반대의 핵심 반론</h2></div>
          <ol>{bearClaims.map((claim) => <li key={claim}>{claim}</li>)}</ol>
          <p className="confidence">주장 신뢰도 {bear?.confidence ?? 0.7} · 반론은 삭제하지 않음</p>
        </article>
      </section>
      <section className="committee-lower">
        <article className="panel evidence-panel">
          <div className="panel-heading"><div><span className="eyebrow">PROVENANCE</span><h2>연결된 근거</h2></div><span>{liveEvidence.length}건</span></div>
          {liveEvidence.map((item) => <div className="evidence-row" key={item.id}><span className="source-chip">{item.source}</span><div><strong>{item.title}</strong><small>{item.published_at.slice(0, 16)} · 검증됨</small></div><span className="verified">✓</span></div>)}
        </article>
        <article className="panel risk-decision">
          <span className="eyebrow">RISK VERDICT · 김안전</span><h2>{cycle?.risk.result ?? '최종 판정 대기'}</h2>
          <p>{cycle?.decision.reason ?? '직원 보고서가 완성되어야 위험 판정을 시작합니다.'}</p>
          <div className="risk-facts"><span>연구 수량 <b>{cycle?.risk.quantity ?? 0}주</b></span><span>위험예산 <b>{formatKrw(cycle?.risk.risk_budget_krw ?? 0)}</b></span><span>무효화 <b>{formatKrw(cycle?.risk.invalidation_price_krw ?? 0)}</b></span></div>
          <button className="primary-button" onClick={() => onDecision('R0 연구 단계이므로 실제 주문은 0건이며 주문 금고는 잠겨 있습니다.')}>김투자 결정 확인</button>
        </article>
      </section>
    </>
  )
}

function AgentOffice({ cycle, ownerToken, onNotice }: {
  cycle: ResearchCycle | null
  ownerToken: string
  onNotice: (message: string) => void
}) {
  const [news, setNews] = useState<NewsStatus | null>(null)
  const [newsBusy, setNewsBusy] = useState<string | null>('loading')
  const [newsError, setNewsError] = useState('')

  const loadNews = async () => {
    try {
      setNews(await fetchNewsStatus())
      setNewsError('')
    } catch (error) {
      setNewsError(error instanceof Error ? error.message : '김뉴스 상태를 불러오지 못했습니다.')
    } finally {
      setNewsBusy(null)
    }
  }

  useEffect(() => {
    void loadNews()
    const timer = window.setInterval(() => void loadNews(), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const handlePoll = async () => {
    setNewsBusy('poll')
    try {
      const result = await pollNews()
      await loadNews()
      onNotice(result?.state === 'PASS' ? '김뉴스가 OpenDART 공식 공시를 갱신했습니다.' : '김뉴스 수집이 실패 폐쇄됐습니다.')
    } catch (error) {
      setNewsError(error instanceof Error ? error.message : '공시 갱신에 실패했습니다.')
      setNewsBusy(null)
    }
  }

  const handleAcknowledge = async (eventId: string) => {
    if (!ownerToken) {
      onNotice('그림자 주문 화면에서 소유자 승인 토큰을 먼저 입력하세요.')
      return
    }
    setNewsBusy(eventId)
    try {
      await acknowledgeNewsEvent(eventId, ownerToken)
      await loadNews()
      onNotice('공시 확인 기록을 감사 원장에 저장했습니다.')
    } catch (error) {
      setNewsError(error instanceof Error ? error.message : '공시 확인 기록에 실패했습니다.')
      setNewsBusy(null)
    }
  }

  const novaState: AgentState = news?.agent.state === 'WORKING'
    ? 'WORKING'
    : news?.state === 'READY'
      ? 'DONE'
      : news?.state === 'NEVER_RUN'
        ? 'WAITING'
        : 'CONFLICT'
  const officeAgents = agents.map((agent) => {
    const report = cycle?.reports.find((item) => item.code === agent.code)
    if (agent.code === 'NOVA' && news) {
      return { ...agent, state: novaState, summary: news.agent.summary, speech: news.agent.summary }
    }
    return report ? { ...agent, state: report.state, summary: report.summary, speech: report.summary } : agent
  })
  return (
    <>
      <section className="page-heading"><div><span className="eyebrow">가상회사 · 실시간 사무실</span><h1>직원 사무실과 도감</h1></div><span className="timestamp">직원을 눌러 지금 하는 일을 확인하세요</span></section>
      <OfficeWorld cycle={cycle} />
      <section className="newsroom panel" aria-labelledby="newsroom-title">
        <div className="panel-heading">
          <div><span className="eyebrow">NOVA · 공식 공시 감시</span><h2 id="newsroom-title">김뉴스의 뉴스룸</h2></div>
          <button className="secondary-button" onClick={handlePoll} disabled={newsBusy !== null}>
            {newsBusy === 'poll' ? '공시 확인 중…' : '지금 공식 공시 확인'}
          </button>
        </div>
        {newsError && <p className="research-error" role="alert">{newsError}</p>}
        {!news && newsBusy === 'loading' && <div className="news-empty">김뉴스가 출근 기록을 확인하고 있습니다…</div>}
        {news && (
          <>
            <div className="news-stats">
              <span><small>감시 상태</small><strong>{news.state}</strong></span>
              <span><small>수집 공시</small><strong>{news.counts.total}건</strong></span>
              <span><small>미확인</small><strong>{news.counts.unresolved}건</strong></span>
              <span><small>매수 차단</small><strong>{news.counts.active_buy_blocks}건</strong></span>
            </div>
            <div className="news-sources" aria-label="김뉴스 데이터 출처">
              {news.sources.map((source) => (
                <span key={source.code} className={source.state === 'ACTIVE' ? 'is-active' : ''}>
                  {source.name} · {source.state === 'ACTIVE' ? '연결됨' : '미연결'}
                </span>
              ))}
              <span>{news.active_window_kst} · {news.poll_interval_seconds / 60}분 간격</span>
            </div>
            {news.events.length === 0 ? (
              <div className="news-empty"><strong>아직 새 공식 공시가 없습니다.</strong><span>일반 뉴스 공급자는 미연결이며 OpenDART만 자동 감시합니다.</span></div>
            ) : (
              <div className="news-event-list">
                {news.events.slice(0, 20).map((event) => (
                  <article className={`news-event severity-${event.severity.toLowerCase()}`} key={event.id}>
                    <div className="news-event-head">
                      <span>{event.severity}</span>
                      <small>{event.company_name} · {event.symbol} · {event.published_at.slice(0, 10)}</small>
                    </div>
                    <h3>{event.title}</h3>
                    <p>{event.analysis.rationale}</p>
                    <div className="news-event-actions">
                      <a href={event.url} target="_blank" rel="noreferrer">DART 원문 열기</a>
                      {event.review_required && !event.acknowledged_at && (
                        <button
                          className="secondary-button"
                          onClick={() => handleAcknowledge(event.id)}
                          disabled={newsBusy !== null}
                        >
                          {newsBusy === event.id ? '기록 중…' : event.severity === 'CRITICAL' ? '확인 기록 · 차단 유지' : '확인 완료'}
                        </button>
                      )}
                      {event.acknowledged_at && <span className="done-mark">확인 기록됨</span>}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </>
        )}
      </section>
      <div className="collection-heading"><div><span className="pixel-kicker">직원 도감</span><h2>오늘 출근한 직원들</h2></div><span>9 / 9 발견</span></div>
      <section className="agent-grid">
        {officeAgents.map((agent) => (
          <article className="agent-card panel" key={agent.code}>
            <div className="agent-card-head"><AgentAvatar agent={agent} /><StatusPill state={agent.state} /></div>
            <span className="eyebrow">{agent.room} · 직원코드 {agent.code}</span>
            <h2>{agent.name}</h2>
            <span className="agent-job">{agent.nickname} · {agent.role}</span>
            <p>{agent.summary}</p>
            <blockquote>“{agent.speech}”</blockquote>
            <div className="agent-meta"><span>마지막 갱신</span><strong>{agent.updated}</strong></div>
          </article>
        ))}
      </section>
    </>
  )
}

const shadowStateLabel: Record<string, string> = {
  CREATED: '주문안 생성',
  PRECHECKED: '위험 사전점검',
  READY: '그림자 준비',
  SHADOW_SUBMITTED: '가상 제출',
  SHADOW_FILLED: '가상 체결',
  SHADOW_CANCELLED: '가상 취소',
}

function Orders({
  cycle,
  execution,
  ownerToken,
  onOwnerToken,
  onExecutionChanged,
}: {
  cycle: ResearchCycle | null
  execution: ExecutionStatus | null
  ownerToken: string
  onOwnerToken: (value: string) => void
  onExecutionChanged: (value: ExecutionStatus) => void
}) {
  const [kiwoom, setKiwoom] = useState<KiwoomStatus | null>(null)
  const [orders, setOrders] = useState<ShadowOrder[]>([])
  const [intents, setIntents] = useState<LiveOrderIntent[]>([])
  const [pilot, setPilot] = useState<PilotStatus | null>(null)
  const [liveClose, setLiveClose] = useState<LiveCloseAuctionStatus | null>(null)
  const [quoteSymbol, setQuoteSymbol] = useState('')
  const [busy, setBusy] = useState<'status' | 'sync' | 'create' | string | null>('status')
  const [message, setMessage] = useState('')
  const refresh = async (signal?: AbortSignal) => {
    const [status, shadowOrders, pilotCandidates, liveIntents, executionState, pilotState, liveCloseState] = await Promise.all([
      fetchKiwoomStatus(signal),
      fetchShadowOrders(signal),
      fetchPilotCandidates(signal),
      fetchLiveIntents(signal),
      fetchExecutionStatus(signal),
      fetchPilotStatus(signal),
      fetchLiveCloseAuctionStatus(signal),
    ])
    setKiwoom(status)
    setOrders([...pilotCandidates, ...shadowOrders].filter((order, index, items) => items.findIndex((item) => item.id === order.id) === index))
    setIntents(liveIntents)
    setPilot(pilotState)
    setLiveClose(liveCloseState)
    onExecutionChanged(executionState)
  }

  useEffect(() => {
    const controller = new AbortController()
    refresh(controller.signal)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setMessage(error instanceof Error ? error.message : '연결 상태를 불러오지 못했습니다.')
      })
      .finally(() => setBusy(null))
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => {
      const now = new Date()
      const minute = now.getHours() * 60 + now.getMinutes()
      if (minute < 15 * 60 || minute > 15 * 60 + 35) return
      fetchLiveCloseAuctionStatus().then(setLiveClose).catch(() => undefined)
    }, 15_000)
    return () => window.clearInterval(timer)
  }, [])

  const handleCloseAuctionTick = async () => {
    setBusy('close-auction-tick'); setMessage('')
    try {
      const result = await runCloseAuctionPilotTick()
      setLiveClose(await fetchLiveCloseAuctionStatus())
      await refresh()
      setMessage(result.action === 'INTENT_READY'
        ? '종가 후보의 최신 호가·계좌 대사가 끝났고 5분 승인 대기 주문안이 준비됐습니다.'
        : result.reason ?? `현재 단계 결과: ${result.action}`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '종가매매 현재 단계를 실행하지 못했습니다.')
    } finally { setBusy(null) }
  }

  const handleSync = async () => {
    setBusy('sync'); setMessage('')
    try {
      const snapshot = await syncKiwoomAccount()
      setMessage(`${snapshot.account_alias} 읽기 전용 스냅샷을 저장했습니다.`)
      setKiwoom(await fetchKiwoomStatus())
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '계좌 조회에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const handleCreate = async () => {
    setBusy('create'); setMessage('')
    try {
      const order = await createShadowOrder(cycle?.id ?? 'latest-cycle')
      setOrders((current) => [order, ...current.filter((item) => item.id !== order.id)])
      setMessage('김주문이 브로커로 보내지 않는 그림자 주문안을 만들었습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '그림자 주문을 만들지 못했습니다.')
    } finally { setBusy(null) }
  }

  const handleCreateOfficial = async () => {
    setBusy('create-official'); setMessage('')
    try {
      const order = await createOfficialShadowOrder(cycle?.id ?? 'latest-cycle')
      setOrders((current) => [order, ...current.filter((item) => item.id !== order.id)])
      setMessage('공식 키움 시세와 실제 종목코드를 사용한 그림자 주문안을 만들었습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '공식 시세 주문안을 만들지 못했습니다.')
    } finally { setBusy(null) }
  }

  const handleSimulate = async (orderId: string) => {
    setBusy(orderId); setMessage('')
    try {
      const order = await simulateShadowOrder(orderId)
      setOrders((current) => current.map((item) => item.id === order.id ? order : item))
      setMessage(order.state === 'SHADOW_FILLED' ? '다음 장 가상 체결을 기록했습니다.' : '갭 보호 규칙으로 가상 주문을 취소했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '가상 체결에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const handleSettleOfficial = async (orderId: string) => {
    setBusy(orderId); setMessage('')
    try {
      const order = await settleOfficialShadowOrder(orderId)
      setOrders((current) => current.map((item) => item.id === order.id ? order : item))
      setMessage(order.state === 'SHADOW_FILLED' ? '공식 다음 거래일 시세로 그림자 체결을 기록했습니다.' : '공식 시세와 갭 규칙으로 그림자 주문을 취소했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '공식 시세 가상 체결에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const handleQuoteSync = async () => {
    if (!/^\d{6}$/.test(quoteSymbol)) {
      setMessage('국내 종목코드 숫자 6자리를 입력하세요.'); return
    }
    setBusy('quote'); setMessage('')
    try {
      await syncOfficialQuote(quoteSymbol)
      setMessage(`${quoteSymbol} 키움 공식 시세 스냅샷을 저장했습니다.`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '공식 시세 조회에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const handleReconciliation = async () => {
    setBusy('reconcile'); setMessage('')
    try {
      const result = await runAccountReconciliation() as { status?: string }
      onExecutionChanged(await fetchExecutionStatus())
      setPilot(await fetchPilotStatus())
      setMessage(result.status === 'PASS' ? '실계좌와 MoneyGun 관리 포지션 대사가 일치합니다.' : '잔고 차이가 발견되어 신규 주문을 중지했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '계좌 대사에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const nextStage = execution?.control.stage === 'R0' ? 'L0' : null
  const handleTransition = async () => {
    if (!nextStage) return
    setBusy('transition'); setMessage('')
    try {
      await transitionExecutionStage(nextStage, ownerToken)
      const updated = await fetchExecutionStatus(); onExecutionChanged(updated)
      setMessage(`${nextStage} 단계로 승격했습니다. 자동 주문은 별도 승인 전까지 꺼져 있습니다.`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '승격 조건을 확인하지 못했습니다.')
    } finally { setBusy(null) }
  }

  const handleGenerateReviews = async () => {
    setBusy('pilot-review'); setMessage('')
    try {
      const updated = await generatePilotReviews(ownerToken)
      setPilot(updated)
      setMessage(updated.reviews.length ? '도래한 검토 후보를 불변 기록으로 생성했습니다. 버전은 자동 적용되지 않습니다.' : '아직 15거래일 검토 시점이 아닙니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '파일럿 검토 기록을 만들지 못했습니다.')
    } finally { setBusy(null) }
  }

  const handleAutomation = async () => {
    if (!execution) return
    setBusy('automation'); setMessage('')
    try {
      await setExecutionAutomation(!execution.control.automation_enabled, ownerToken)
      const updated = await fetchExecutionStatus(); onExecutionChanged(updated)
      setMessage(updated.control.automation_enabled ? 'L0-AUTO-v1 무인 실주문을 켰습니다. 이후 생성되는 주문안만 자동 처리합니다.' : '자동 실행을 중지했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '자동 실행 설정에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const handleCreateIntent = async (shadowOrderId: string) => {
    setBusy(`intent-${shadowOrderId}`); setMessage('')
    try {
      const intent = await createLiveIntent(shadowOrderId)
      setIntents((current) => [intent, ...current.filter((item) => item.id !== intent.id)])
      setMessage(intent.state === 'REJECTED' ? intent.precheck_blockers.join(' ') : '실행 가디언이 실주문안을 사전점검했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '실주문안을 만들지 못했습니다.')
    } finally { setBusy(null) }
  }

  const handleApproveIntent = async (intent: LiveOrderIntent) => {
    setBusy(`approve-${intent.id}`); setMessage('')
    try {
      const updated = await approveLiveIntent(intent.id, intent.scope_hash, ownerToken)
      setIntents((current) => current.map((item) => item.id === updated.id ? updated : item))
      setMessage('5분 유효 사용자 승인을 불변 감사 기록에 저장했습니다.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '주문 승인에 실패했습니다.')
    } finally { setBusy(null) }
  }

  const handleSubmitIntent = async (intent: LiveOrderIntent) => {
    setBusy(`submit-${intent.id}`); setMessage('')
    try {
      const updated = await submitLiveIntent(intent.id, ownerToken)
      setIntents((current) => current.map((item) => item.id === updated.id ? updated : item))
      setMessage(updated.state === 'UNKNOWN' ? '응답 불명입니다. 재전송하지 않고 키움 조회를 기다립니다.' : `키움 주문 상태: ${updated.state}`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '실주문 제출이 차단됐습니다.')
    } finally { setBusy(null) }
  }

  const handleCancelIntent = async (intent: LiveOrderIntent) => {
    setBusy(`cancel-${intent.id}`); setMessage('')
    try {
      const updated = await cancelLiveIntent(intent.id, ownerToken)
      setIntents((current) => current.map((item) => item.id === updated.id ? updated : item))
      setMessage(updated.state === 'CANCEL_UNKNOWN' ? '취소 응답 불명입니다. 재전송하지 않고 키움 조회를 기다립니다.' : `키움 취소 상태: ${updated.state}`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '주문 취소가 차단됐습니다.')
    } finally { setBusy(null) }
  }

  const submittedCount = intents.filter((intent) => ['ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_SUBMITTING', 'CANCEL_UNKNOWN', 'CANCEL_ACKNOWLEDGED'].includes(intent.state)).length

  return (
    <>
      <section className="page-heading"><div><span className="eyebrow">김주문의 실행 금고</span><h1>키움 조회와 그림자 주문</h1></div><span className="large-status safe">실주문 {submittedCount}건</span></section>
      <section className="broker-shadow-grid">
        <article className="panel broker-card">
          <div className="panel-heading"><div><span className="eyebrow">키움증권 · 조회만 허용</span><h2>읽기 전용 연결</h2></div><span className={`connection-chip ${kiwoom?.configured ? 'is-ready' : ''}`}>{kiwoom?.configured ? '키 준비됨' : '설정 필요'}</span></div>
          <p>계좌평가·공식 시세·미체결·체결만 가져옵니다. 이 조회 모듈에는 매수·매도·정정·취소 기능이 없습니다.</p>
          <div className="broker-facts"><span>접속 환경 <b>{kiwoom?.environment === 'production' ? '운영계좌 조회' : '모의투자 조회'}</b></span><span>접속 방식 <b>OAuth 조회 전용</b></span><span>주문 권한 <b className="negative">없음</b></span><span>토큰 저장 <b>메모리만</b></span></div>
          {kiwoom?.blockers.map((blocker) => <small className="setup-note" key={blocker}>{blocker}</small>)}
          <div className="data-tools">
            <label><span>공식 시세 종목코드</span><input value={quoteSymbol} inputMode="numeric" maxLength={6} placeholder="예: 005930" onChange={(event) => setQuoteSymbol(event.target.value.replace(/\D/g, '').slice(0, 6))} /></label>
            <button className="secondary-button" onClick={handleQuoteSync} disabled={!kiwoom?.configured || busy !== null}>{busy === 'quote' ? '시세 조회 중…' : '공식 시세 저장'}</button>
          </div>
          <div className="order-actions"><button className="secondary-button" onClick={() => { setBusy('status'); refresh().catch(() => setMessage('상태 새로고침에 실패했습니다.')).finally(() => setBusy(null)) }} disabled={busy !== null}>상태 새로고침</button><button className="secondary-button" onClick={handleReconciliation} disabled={!kiwoom?.configured || busy !== null}>{busy === 'reconcile' ? '대사 중…' : '계좌 대사'}</button><button className="primary-button" onClick={handleSync} disabled={!kiwoom?.configured || busy !== null}>{busy === 'sync' ? '조회 중…' : '계좌 동기화'}</button></div>
        </article>
        <article className="panel shadow-create-card">
          <div className="panel-heading"><div><span className="eyebrow">브로커 전송 없음</span><h2>그림자 주문 만들기</h2></div><span className="lock-mark">🔒</span></div>
          <p>최신 투자위원회 결정과 김안전의 수량·무효화 가격으로 로컬 주문안을 만듭니다.</p>
          <div className="shadow-guard"><strong>항상 적용되는 보호 규칙</strong><span>시초가 갭 3% 초과 취소 · 1~3% 수량 절반 · 실주문 API 호출 0회</span></div>
          <div className="stacked-actions">
            <button className="primary-button full-button" onClick={handleCreate} disabled={!cycle || busy !== null}>{busy === 'create' ? '김주문이 작성 중…' : '재현 데이터 그림자 주문'}</button>
            <button className="secondary-button full-button" onClick={handleCreateOfficial} disabled={!cycle || !kiwoom?.configured || busy !== null}>{busy === 'create-official' ? '공식 주문안 작성 중…' : '공식 시세 그림자 주문'}</button>
          </div>
        </article>
      </section>
      <section className="panel wide-panel close-live-console" aria-label="종가매매 전시장 자동 스캔">
        <div className="panel-heading">
          <div><span className="eyebrow">CLOSE-AUCTION-KR-v3-L0</span><h2>오늘 종가매매 전시장</h2></div>
          <span className={`connection-chip ${liveClose?.state === 'BUY_CANDIDATE' ? 'is-ready' : ''}`}>{liveClose?.state === 'BUY_CANDIDATE' ? '후보 발견' : liveClose?.state === 'HOLD' ? '조건 미달 · 보류' : liveClose?.state === 'SCAN_MISSED' ? '스캔 시간 놓침' : liveClose?.state === 'WINDOW_ENDED' ? '오늘 주문창 종료' : liveClose?.state === 'MARKET_CLOSED' ? '휴장일' : '15:05 대기'}</span>
        </div>
        <div className="close-live-flow">
          <span><b>15:05</b><small>KOSPI·KOSDAQ 거래대금 스캔</small></span>
          <i>→</i><span><b>15:20</b><small>최신 호가·계좌 대사</small></span>
          <i>→</i><span><b>{execution?.control.automation_enabled ? '자동 승인·제출' : '5분 승인'}</b><small>{execution?.control.automation_enabled ? '가디언 재검사 통과 시' : '주인 확인 후에만 제출'}</small></span>
        </div>
        <div className="close-live-metrics">
          <article><small>순위 대조</small><strong>{liveClose?.cycle?.coverage.coverage.ranking_rows ?? 0}행</strong><span>KOSPI·KOSDAQ 보통주</span></article>
          <article><small>1주 매수 가능</small><strong>{liveClose?.cycle?.coverage.coverage.affordable_rows ?? 0}종목</strong><span>2,000~45,000원</span></article>
          <article><small>상세 분석</small><strong>{liveClose?.cycle?.coverage.evaluated_count ?? 0}종목</strong><span>일봉·시총·유동성</span></article>
          <article><small>소형주 분석</small><strong>{liveClose?.cycle?.coverage.small_cap_evaluated_count ?? 0}종목</strong><span>500억~1조원</span></article>
        </div>
        {liveClose?.cycle ? <div className="close-live-decision">
          <div><span className="symbol-chip">{liveClose.cycle.decision.symbol || 'HOLD'}</span><strong>{liveClose.cycle.decision.name}</strong><small>{liveClose.cycle.decision.reason}</small></div>
          <div><b>{liveClose.cycle.decision.score}점</b><small>{liveClose.cycle.decision.quantity}주 후보 · {execution?.control.automation_enabled ? '새 주문안 자동 처리' : '수동 승인 대기'}</small></div>
        </div> : <p className="muted">거래일 15:05~15:10에 전시장 후보를 만들고, 통과 종목이 없으면 주문하지 않습니다.</p>}
        <div className="order-actions"><button className="secondary-button" onClick={handleCloseAuctionTick} disabled={!kiwoom?.configured || busy !== null}>{busy === 'close-auction-tick' ? '현재 단계 실행 중…' : '현재 단계 실행·새로고침'}</button></div>
      </section>
      <section className="panel wide-panel pilot-console" aria-label="5만 원 실거래 파일럿">
        <div className="panel-heading">
          <div><span className="eyebrow">L0 PILOT · {pilot?.active_version ?? 'v1.0'}</span><h2>5만 원 실거래 실험실</h2></div>
          <span className={`connection-chip ${execution?.control.stage === 'L0' && !execution.control.kill_switch_active ? 'is-ready' : ''}`}>{execution?.control.stage === 'L0' ? 'L0 운용 중' : '시작 잠금'}</span>
        </div>
        <div className="pilot-warning"><strong>전략 수익성 미검증</strong><span>실제 5만 원 전액을 잃을 수 있습니다. {execution?.control.automation_enabled ? 'L0-AUTO-v1 위임으로 새 주문안은 별도 클릭 없이 제출됩니다.' : '현재는 매 주문마다 주인 승인이 필요합니다.'}</span></div>
        <div className="pilot-metrics">
          <article><small>평가금액</small><strong>{formatKrw(pilot?.risk.marked_equity_krw ?? 50_000)}</strong><span className={(pilot?.risk.total_pnl_krw ?? 0) >= 0 ? 'positive' : 'negative'}>누적 {formatKrw(pilot?.risk.total_pnl_krw ?? 0)}</span></article>
          <article><small>오늘 방어손익</small><strong className={(pilot?.risk.daily_defense_pnl_krw ?? 0) >= 0 ? 'positive' : 'negative'}>{formatKrw(pilot?.risk.daily_defense_pnl_krw ?? 0)}</strong><span>-{formatKrw(pilot?.limits.daily_loss_krw ?? 1_500)} 도달 시 신규매수 중지</span></article>
          <article><small>실거래일</small><strong>{pilot?.progress.operating_days ?? 0}/60일</strong><span>다음 검토 {pilot?.progress.next_review_day ? `${pilot.progress.next_review_day}일` : 'v2 후보 생성됨'}</span></article>
          <article><small>오늘 신규진입</small><strong>{pilot?.progress.entries_today ?? 0}/{pilot?.limits.max_daily_entries ?? 3}회</strong><span>1종목 · 주문당 최대 {formatKrw(pilot?.limits.order_budget_krw ?? 45_000)}</span></article>
        </div>
        <div className="pilot-review-row">
          {[15, 30, 45, 60].map((day) => {
            const review = pilot?.reviews.find((item) => item.checkpoint_day === day)
            return <span className={review ? 'is-done' : ''} key={day}><b>{day}일</b><small>{review ? `${review.candidate_version} · 주인 검토 대기` : '아직 도래 전'}</small></span>
          })}
          <button className="secondary-button" onClick={handleGenerateReviews} disabled={!ownerToken || busy !== null}>{busy === 'pilot-review' ? '기록 생성 중…' : '도래한 버전 검토 생성'}</button>
        </div>
        <p className="muted">15·30·45일에는 v1.x 개선 후보, 60일에는 v2.0 후보만 만듭니다. 규칙 변경·자본 증액은 자동으로 실행되지 않습니다.</p>
      </section>
      {message && <div className="inline-feedback" role="status">{message}</div>}
      <section className="panel wide-panel execution-control-panel" aria-label="P5부터 P7 실행 가디언">
        <div className="panel-heading">
          <div><span className="eyebrow">P5–P7 · EXECUTION GUARDIAN</span><h2>실거래 다중 잠금과 단계 승격</h2></div>
          <span className={`connection-chip ${execution?.guardian.configured ? 'is-ready' : ''}`}>{execution?.guardian.configured ? '실행 잠금 준비됨' : '실행 잠김'}</span>
        </div>
        <div className="stage-track" aria-label="운영 단계">
          {['R0 시작 잠금', 'L0 5만원 실거래', 'L1 검증 후 확대', 'L2 제한 자율'].map((label, index) => {
            const current = ['R0', 'L0', 'L1', 'L2'].indexOf(execution?.control.stage ?? 'R0')
            return <span className={index === current ? 'is-current' : index < current ? 'is-past' : ''} key={label}><i>{index + 1}</i><b>{label}</b></span>
          })}
        </div>
        <div className="execution-summary">
          <article><small>현재 단계</small><strong>{execution?.control.stage ?? 'R0'}</strong><span>{execution?.control.stage === 'R0' ? '외부 잠금 확인 전' : execution?.control.stage === 'L0' ? `5만원 · ${execution.control.automation_enabled ? '위임 자동 주문' : '주문마다 승인'}` : execution?.control.stage === 'L1' ? '검증 후 확대 운용' : '제한 규칙 내 자동화'}</span></article>
          <article><small>긴급 중지</small><strong className={execution?.control.kill_switch_active ? 'negative' : 'positive'}>{execution?.control.kill_switch_active ? '활성' : '정상'}</strong><span>중지 시 신규 주문·자동화 차단</span></article>
          <article><small>주문 환경</small><strong>{execution?.guardian.environment === 'production' ? '운영계좌' : '모의 환경'}</strong><span>L0 한도 {formatKrw(execution?.guardian.l0_capital_limit_krw ?? 50_000)}</span></article>
          <article><small>L0 무인 실주문</small><strong className={execution?.control.automation_enabled ? 'negative' : ''}>{execution?.control.automation_enabled ? '켜짐' : '꺼짐'}</strong><span>단일 리더·중복 방지·가디언 유지</span></article>
        </div>
        <div className="gate-grid">
          {(['L0_TO_L1', 'R1_TO_L1', 'L1_TO_L2'] as const).map((gateKey) => {
            const gate = execution?.gates[gateKey]
            const metrics = gate?.metrics ?? {}
            const isPilot = gateKey === 'L0_TO_L1'
            const isShadow = gateKey === 'R1_TO_L1'
            return <article className="gate-card" key={gateKey}>
              <div><span>{isPilot ? 'L0 → v2 후보' : isShadow ? 'R1 → L1' : 'L1 → L2'}</span><b className={gate?.eligible ? 'positive' : 'negative'}>{gate?.eligible ? '통과' : '대기'}</b></div>
              <p>{isPilot ? `5만원 실거래 ${metrics.operating_days ?? 0}/60일 · 검토 ${metrics.completed_reviews ?? 0}/4회` : isShadow ? `공식 그림자 ${metrics.operating_days ?? 0}/60일 · ${metrics.decisions ?? 0}/100결정` : `승인형 실거래 ${metrics.operating_days ?? 0}/60일 · ${metrics.fills ?? 0}/100체결`}</p>
              <div className="gate-progress"><i style={{ width: `${Math.min(100, ((metrics.operating_days ?? 0) / 60) * 100)}%` }} /></div>
              <small>{Object.values(gate?.gates ?? {}).filter(Boolean).length}/{Object.keys(gate?.gates ?? {}).length} 안전 조건 충족</small>
            </article>
          })}
        </div>
        <div className="guardian-blockers">
          <strong>현재 주문 차단 사유</strong>
          {execution?.guardian.blockers.length ? execution.guardian.blockers.map((blocker) => <span key={blocker}>• {blocker}</span>) : <span className="positive">모든 환경 잠금이 열렸습니다. 단계 규칙은 계속 적용됩니다.</span>}
        </div>
        <div className="owner-control-row">
          <label><span>소유자 승인 토큰 · 저장 안 함</span><input type="password" autoComplete="off" value={ownerToken} placeholder="환경변수와 같은 24자 이상 토큰" onChange={(event) => onOwnerToken(event.target.value)} /></label>
          <button className="secondary-button" onClick={handleTransition} disabled={!nextStage || !ownerToken || busy !== null}>{busy === 'transition' ? '게이트 확인 중…' : nextStage ? '5만원 L0 시작 승인' : 'L0 운용 중'}</button>
          <button className={execution?.control.automation_enabled ? 'danger-button' : 'secondary-button'} onClick={handleAutomation} disabled={!['L0', 'L2'].includes(execution?.control.stage ?? '') || !ownerToken || busy !== null}>{execution?.control.automation_enabled ? '무인 실주문 끄기' : 'L0 무인 실주문 켜기'}</button>
        </div>
        <p className="muted">자동화를 켜면 활성화 이후 생성된 주문안만 위임 승인됩니다. 뉴스 차단·계좌 대사·수량 한도·손실 중지·킬 스위치·응답 불명 재전송 금지는 계속 적용됩니다.</p>
      </section>
      <section className="panel wide-panel shadow-list-panel">
        <div className="panel-heading"><div><span className="eyebrow">그림자 주문 기록</span><h2>김주문 작업대</h2></div><span>{orders.length}건 · 브로커 전송 0건</span></div>
        {busy === 'status' && <p className="orders-placeholder">기록을 불러오는 중입니다…</p>}
        {busy !== 'status' && orders.length === 0 && <div className="orders-placeholder"><strong>아직 그림자 주문이 없습니다.</strong><span>투자위원회 분석 후 위 버튼으로 첫 주문안을 만들어보세요.</span></div>}
        <div className="shadow-order-list">
          {orders.map((order) => (
            <article className="shadow-order-row" key={order.id}>
              <div><span className="symbol-chip">{order.symbol}</span><strong>{order.name}</strong><small>{order.next_session_date} 가상 실행 · {order.quantity}주 · {order.simulation_source === 'KIWOOM_CLOSE_AUCTION_QUOTE' ? '종가 단일가 공식 시세' : order.simulation_source === 'KIWOOM_OFFICIAL_QUOTE' ? '공식 시세' : '재현 픽스처'}</small></div>
              <div className="order-price"><span>지정가</span><b>{formatKrw(order.limit_price_krw)}</b><small>무효화 {formatKrw(order.invalidation_price_krw)}</small></div>
              <div className="order-state"><span>{shadowStateLabel[order.state] ?? order.state}</span><small>{order.fill ? `${order.fill.quantity}주 · ${formatKrw(order.fill.price_krw)}` : '실계좌 영향 없음'}</small></div>
              <div className="row-actions">
                {order.state === 'READY' && <button className="secondary-button" onClick={() => order.simulation_source === 'KIWOOM_OFFICIAL_QUOTE' ? handleSettleOfficial(order.id) : handleSimulate(order.id)} disabled={busy !== null}>{busy === order.id ? '처리 중…' : order.simulation_source === 'KIWOOM_OFFICIAL_QUOTE' ? '공식 시세 정산' : '재현 가상 체결'}</button>}
                {order.simulation_source.startsWith('KIWOOM_') && <button className="secondary-button" onClick={() => handleCreateIntent(order.id)} disabled={busy !== null || order.state !== 'READY'}>{busy === `intent-${order.id}` ? '사전점검 중…' : 'L0 실주문안 사전점검'}</button>}
                {order.state !== 'READY' && order.simulation_source !== 'KIWOOM_OFFICIAL_QUOTE' && <span className="done-mark">✓ 기록 완료</span>}
              </div>
            </article>
          ))}
        </div>
      </section>
      <section className="panel wide-panel live-intent-panel">
        <div className="panel-heading"><div><span className="eyebrow">L0 소액 승인 · 이중 그림자 기록</span><h2>실주문 의도 금고</h2></div><span>{intents.length}건</span></div>
        {intents.length === 0 && <div className="orders-placeholder"><strong>실주문 의도가 없습니다.</strong><span>공식 시세 그림자 기록만 가디언 사전점검에 들어갈 수 있습니다.</span></div>}
        <div className="live-intent-list">
          {intents.map((intent) => <article className="live-intent-row" key={intent.id}>
            <div><span className="symbol-chip">{intent.symbol}</span><strong>{intent.name}</strong><small>{intent.stage} · {intent.quantity}주 · {formatKrw(intent.order_value_krw)} · {intent.performance_qualified ? 'OOS 통과' : '수익성 미검증'}</small></div>
            <div><b className={intent.state === 'UNKNOWN' || intent.state === 'REJECTED' ? 'negative' : ''}>{intent.state}</b><small>{intent.precheck_blockers.length ? intent.precheck_blockers.join(' · ') : '사전점검 통과'}</small></div>
            <div className="row-actions">
              {intent.state === 'AWAITING_APPROVAL' && <button className="secondary-button" onClick={() => handleApproveIntent(intent)} disabled={!ownerToken || busy !== null}>{busy === `approve-${intent.id}` ? '승인 저장 중…' : '5분 승인'}</button>}
              {intent.state === 'READY' && <button className="danger-button" onClick={() => handleSubmitIntent(intent)} disabled={!ownerToken || busy !== null}>{busy === `submit-${intent.id}` ? '제출 중…' : '실주문 제출'}</button>}
              {['ACKNOWLEDGED', 'PARTIALLY_FILLED'].includes(intent.state) && <button className="danger-button" onClick={() => handleCancelIntent(intent)} disabled={!ownerToken || busy !== null}>{busy === `cancel-${intent.id}` ? '취소 제출 중…' : '미체결 취소'}</button>}
              {intent.state === 'UNKNOWN' && <span className="unknown-warning">재전송 금지 · 조회 대기</span>}
              {intent.state === 'CANCEL_UNKNOWN' && <span className="unknown-warning">취소 재전송 금지 · 조회 대기</span>}
            </div>
          </article>)}
        </div>
      </section>
      <section className="panel wide-panel">
        <div className="panel-heading"><div><span className="eyebrow">주문 상태 흐름</span><h2>실거래와 분리된 상태 기계</h2></div></div>
        <div className="state-machine">{['주문안 생성', '위험 사전점검', '그림자 준비', '가상 제출', '가상 체결·취소'].map((state, index) => <span key={state}>{index > 0 && <i>→</i>}<b>{state}</b></span>)}</div>
      </section>
    </>
  )
}

const operationCheckLabels: Record<string, string> = {
  kiwoom_read_configured: '키움 조회 전용 연결',
  open_dart_configured: 'OpenDART 공시 연결',
  official_snapshot_today: '오늘 공식 시장 스냅샷',
  snapshot_quality_pass: '가격 데이터 품질',
  audit_and_ledger_pass: '감사 해시·복식부기',
  backup_verified: '검증된 백업',
  live_trading_locked: '실거래 잠금',
}

const dataQualificationLabels: Record<string, string> = {
  authorized_real_source: '승인된 KRX·계약 데이터',
  minimum_five_year_period: '최소 5년 검증 기간',
  survivorship_bias_controlled: '상장폐지 포함 전체 유니버스',
  historical_designation_states_complete: '과거 투자주의·정지 상태',
  historical_delisted_included: '상장폐지 가격 이력',
  source_artifacts_verified: '원본·정규화 체크섬',
}

const collectionCheckLabels: Record<string, string> = {
  public_price_history_collected: '공공데이터 가격 5년 수집',
  issuance_history_collected: '주식 발행·상장폐지 정보 수집',
  normalized_market_database: '시점 정규화 DB 생성',
  benchmark_history_complete: 'KODEX 200 벤치마크 5년',
  caution_quarter_files: '투자주의 분할 원본',
  caution_period_start_covered: '투자주의 시작일 포함',
  caution_period_end_covered: '투자주의 종료일 포함',
  warning_intervals_present: '투자경고 지정·해제 이력',
  danger_intervals_present: '투자위험 지정·해제 이력',
  managed_intervals_complete: '관리종목 지정·해제 이력',
  watchlist_intervals_complete: '환기종목 지정·해제 이력',
  halted_intervals_complete: '거래정지·재개 이력',
  collection_manifest_invalid: '수집 명세서 무결성',
}

const autonomyLabels: Record<string, string> = {
  closed_trade_attribution: '청산 손익 귀속',
  postmortem_journal: '거래 사후검토 일지',
  champion_challenger_comparison: '전략 대항전',
  partial_fill_reconciliation: '부분체결 대사',
  network_unknown_no_retry: '통신불명 재주문 금지',
  profit_vault_automation: '수익금 금고',
  rolling_expectancy_monitor: '기대값·슬리피지 감시',
  strategy_change_owner_approval: '전략변경 주인 승인',
  deployment_configuration_gate: '배포 구성 관문',
}

const externalLabels: Record<string, string> = {
  qualified_market_data: '5년 시점정합 시장데이터',
  oos_strategy_eligible: 'OOS 전략 자격 통과',
  shadow_60_days_100_decisions: '그림자 60일·100결정',
  production_identity_and_secrets: '운영 인증·비밀관리',
  release_review_approved: '릴리스 점검 승인',
  owner_live_activation: '주인의 실거래 최종 활성화',
}

const deploymentLabels: Record<string, string> = {
  postgresql_configured: '운영 PostgreSQL',
  oidc_issuer_configured: '로그인 발급자',
  oidc_client_configured: '로그인 앱 ID',
  external_secret_manager_configured: '외부 비밀 금고',
  error_monitoring_configured: '오류 감시',
  public_https_origin_configured: 'HTTPS 주소',
  managed_backup_configured: '시점복구 백업',
  windows_host: 'Windows 전용 호스트',
  loopback_only_origin: '내 PC에서만 접속',
  local_database_configured: '로컬 단일 원장 DB',
  windows_dpapi_secrets_loaded: 'Windows 암호화 비밀',
  scheduled_autostart_configured: '로그인 후 자동 시작',
  single_process_lock_acquired: '중복 프로세스 차단',
  sleep_disabled: '절전·최대절전 차단',
  windows_clock_synchronized: '시스템 시각 동기화',
  registered_public_ip_matches: '키움 등록 IP 일치',
  local_logging_configured: '로컬 장애 로그',
  backup_encryption_confirmed: '백업 저장소 암호화',
  database_backup_fresh_24h: '24시간 이내 백업',
  database_restore_drill_fresh_31d: '31일 이내 복원 훈련',
}

function OperationsCenter({ ownerToken }: { ownerToken: string }) {
  const [readiness, setReadiness] = useState<OperationsReadiness | null>(null)
  const [notifications, setNotifications] = useState<OperationsNotification[]>([])
  const [closeAuction, setCloseAuction] = useState<CloseAuctionBundle | null>(null)
  const [openingRange, setOpeningRange] = useState<OpeningRangeBundle | null>(null)
  const [openingCapture, setOpeningCapture] = useState<OpeningRangeCaptureResult | null>(null)
  const [l0Modes, setL0Modes] = useState<L0ModesStatus | null>(null)
  const [openingLive, setOpeningLive] = useState<OpeningRangeLiveStatus | null>(null)
  const [exitStatus, setExitStatus] = useState<L0ExitStatus | null>(null)
  const [dataQualification, setDataQualification] = useState<DataQualificationStatus | null>(null)
  const [autonomy, setAutonomy] = useState<AutonomyReadiness | null>(null)
  const [performance, setPerformance] = useState<PerformanceReport | null>(null)
  const [runtimeMonitor, setRuntimeMonitor] = useState<RuntimeMonitor | null>(null)
  const [comparison, setComparison] = useState<StrategyComparison | null>(null)
  const [deployment, setDeployment] = useState<DeploymentReadiness | null>(null)
  const [activation, setActivation] = useState<ActivationPortfolio | null>(null)
  const [qualificationFile, setQualificationFile] = useState('')
  const [openingArchiveFile, setOpeningArchiveFile] = useState('')
  const [busy, setBusy] = useState<string | null>('load')
  const [message, setMessage] = useState('')

  const reload = async () => {
    const [nextReadiness, nextNotifications, nextCloseAuction, nextOpeningRange, nextQualification, nextAutonomy, nextPerformance, nextMonitor, nextComparison, nextDeployment, nextActivation] = await Promise.all([
      fetchOperationsReadiness(),
      fetchOperationsNotifications(),
      fetchLatestCloseAuction(),
      fetchLatestOpeningRange(),
      fetchDataQualificationStatus(),
      fetchAutonomyReadiness(),
      fetchPerformanceReport(),
      fetchRuntimeMonitor(),
      fetchStrategyComparison(),
      fetchDeploymentReadiness(),
      fetchActivationReadiness(),
    ])
    setReadiness(nextReadiness)
    setNotifications(nextNotifications)
    setCloseAuction(nextCloseAuction)
    setOpeningRange(nextOpeningRange)
    setDataQualification(nextQualification)
    setAutonomy(nextAutonomy)
    setPerformance(nextPerformance)
    setRuntimeMonitor(nextMonitor)
    setComparison(nextComparison)
    setDeployment(nextDeployment)
    setActivation(nextActivation)
    const [nextL0Modes, nextOpeningLive, nextExitStatus] = await Promise.all([
      fetchL0ModesStatus(),
      fetchOpeningRangeLiveStatus(),
      fetchL0ExitStatus(),
    ])
    setL0Modes(nextL0Modes)
    setOpeningLive(nextOpeningLive)
    setExitStatus(nextExitStatus)
  }

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      fetchOperationsReadiness(controller.signal),
      fetchOperationsNotifications(controller.signal),
      fetchLatestCloseAuction(controller.signal),
      fetchLatestOpeningRange(controller.signal),
      fetchPerformanceReport(controller.signal),
      fetchRuntimeMonitor(controller.signal),
      fetchStrategyComparison(controller.signal),
      fetchDeploymentReadiness(controller.signal),
      fetchActivationReadiness(controller.signal),
    ]).then(([nextReadiness, nextNotifications, nextCloseAuction, nextOpeningRange, nextPerformance, nextMonitor, nextComparison, nextDeployment, nextActivation]) => {
      setReadiness(nextReadiness)
      setNotifications(nextNotifications)
      setCloseAuction(nextCloseAuction)
      setOpeningRange(nextOpeningRange)
      setPerformance(nextPerformance)
      setRuntimeMonitor(nextMonitor)
      setComparison(nextComparison)
      setDeployment(nextDeployment)
      setActivation(nextActivation)
      setBusy(null)
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setMessage('운영 상태를 불러오지 못했습니다.')
      setBusy(null)
    })
    fetchDataQualificationStatus(controller.signal)
      .then(setDataQualification)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setMessage('자격 데이터 상태를 불러오지 못했습니다.')
      })
    fetchAutonomyReadiness(controller.signal)
      .then(setAutonomy)
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setMessage('무개입 개발 상태를 불러오지 못했습니다.')
      })
    Promise.all([
      fetchL0ModesStatus(controller.signal),
      fetchOpeningRangeLiveStatus(controller.signal),
      fetchL0ExitStatus(controller.signal),
    ]).then(([nextL0Modes, nextOpeningLive, nextExitStatus]) => {
      setL0Modes(nextL0Modes)
      setOpeningLive(nextOpeningLive)
      setExitStatus(nextExitStatus)
    }).catch((error: unknown) => {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setMessage('다중 모드 L0 상태를 불러오지 못했습니다.')
    })
    return () => controller.abort()
  }, [])

  const act = async (kind: string, action: () => Promise<unknown>, done: string) => {
    setBusy(kind); setMessage('')
    try {
      await action(); await reload(); setMessage(done)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '운영 작업을 완료하지 못했습니다.')
    } finally { setBusy(null) }
  }

  const latest = readiness?.latest_run
  const validation = readiness?.latest_validation
  const unread = notifications.filter((item) => !item.acknowledged_at).length
  const desktopRecoveryPassed = activation?.missions
    .find((item) => item.mode_code === 'L0_PILOT')
    ?.steps.find((step) => step.code === 'desktop_recovery')?.passed ?? false
  return (
    <>
      <section className="page-heading">
        <div><span className="eyebrow">REAL OPERATIONS · FAIL CLOSED</span><h1>운영 센터</h1></div>
        <span className={`large-status ${readiness?.state === 'READY_SHADOW' ? 'safe' : ''}`}>
          {busy === 'load' ? '확인 중' : readiness?.state === 'READY_SHADOW' ? '그림자 운영 준비' : '확인 필요'}
        </span>
      </section>
      <section className="ops-command panel">
        <div>
          <span className="pixel-kicker">김운영 · DAILY CLOSE</span>
          <h2>{latest?.state === 'COMPLETE' ? '오늘 회사 업무가 안전하게 끝났어요' : '오늘의 종가 업무를 준비합니다'}</h2>
          <p>{latest?.summary.decision ? `${latest.summary.decision.name} · ${latest.summary.decision.action} · ${latest.summary.validation_status}` : latest?.summary.reason ?? '계좌 대사부터 직원 회의, OOS 검증, 공식 시세 확인까지 한 번에 실행합니다.'}</p>
        </div>
        <div className="ops-command-actions">
          <button className="primary-button" disabled={busy !== null} onClick={() => act('daily', runDailyOperations, '오늘의 종가 업무를 처리했습니다.')}>
            {busy === 'daily' ? '직원들이 업무 중…' : '오늘 업무 실행'}
          </button>
          <small>장 종료 전에는 자동으로 건너뜁니다 · 실제 주문 0건</small>
        </div>
      </section>
      <section className="mode-lab panel">
        <div className="panel-heading">
          <div><span className="eyebrow">MODE LAB · 독립 자격 심사</span><h2>운용 모드 실험실</h2></div>
          <span className="mode-lock">실주문 잠금</span>
        </div>
        <div className="mode-card-grid">
          <article className="mode-card is-active">
            <div><span className="mode-number">01</span><b>집중투자</b><small>FOCUS-MOMENTUM-KR-v2-L0</small></div>
            <p>전시장 추세·상대강도 · 다음 장 09:05 지정가 · 최대 14일</p>
            <strong className="warning">{l0Modes?.modes.find((item) => item.mode_code === 'FOCUS')?.state ?? 'NOT_RUN'}</strong>
          </article>
          <article className="mode-card close-mode">
            <div><span className="mode-number">02</span><b>종가매매</b><small>CLOSE-AUCTION-KR-v2</small></div>
            <p>전일 확정 신호 · KRX 종가 단일가 지정가 · 5거래일 보유</p>
            <strong className={closeAuction?.validation.promotion_eligible ? 'positive' : 'negative'}>
              {closeAuction ? (closeAuction.validation.promotion_eligible ? '확대 자격 검토 가능' : 'L0에서 수익성 검증') : '아직 검증 안 함'}
            </strong>
          </article>
          <article className="mode-card opening-mode">
            <div><span className="mode-number">03</span><b>장초 단타</b><small>OPEN-RANGE-KR-v2</small></div>
            <p>첫 10분 관찰 · +1.5R부터 고점 추적 · 11시 전량청산</p>
            <strong className="warning">실시간 단계 · {openingLive?.phase ?? 'NOT_STARTED'}</strong>
          </article>
          <article className="mode-card safe-mode">
            <div><span className="mode-number">04</span><b>안전투자</b><small>BALANCED-TREND-KR-v1-L0</small></div>
            <p>대형·고유동성 · 낮은 변동성 · 최대 90일 보유 검토</p>
            <strong className="warning">{l0Modes?.modes.find((item) => item.mode_code === 'BALANCED')?.state ?? 'NOT_RUN'}</strong>
          </article>
          <article className="mode-card long-mode">
            <div><span className="mode-number">05</span><b>장기투자</b><small>LONG-TREND-KR-v1-L0</small></div>
            <p>대형주 장기 추세 · 낮은 변동성 · 1년 보유 검토</p>
            <strong className="warning">{l0Modes?.modes.find((item) => item.mode_code === 'LONG_TERM')?.state ?? 'NOT_RUN'}</strong>
          </article>
        </div>
        <div className="mode-run-actions" aria-label="L0 모드 실행">
          {(['FOCUS', 'BALANCED', 'LONG_TERM'] as const).map((modeCode) => (
            <button
              className="secondary-button"
              disabled={busy !== null}
              key={modeCode}
              onClick={() => act(`l0-${modeCode}`, () => runL0ModeTick(modeCode), `${modeCode} 모드의 현재 시간 단계를 처리했습니다.`)}
            >
              {busy === `l0-${modeCode}` ? '직원들이 확인 중…' : `${modeCode === 'FOCUS' ? '집중' : modeCode === 'BALANCED' ? '안전' : '장기'} 모드 실행`}
            </button>
          ))}
          <button
            className="secondary-button"
            disabled={busy !== null}
            onClick={() => act('opening-live', runOpeningRangePilotTick, '장초 실시간 수집·신호 단계를 처리했습니다.')}
          >
            {busy === 'opening-live' ? '20초 실시간 수집 중…' : '장초 실시간 단계 실행'}
          </button>
          <button
            className="secondary-button"
            disabled={busy !== null}
            onClick={() => act('exit-guard', runL0ExitTick, 'MoneyGun 관리 보유종목의 청산 조건을 검사했습니다.')}
          >
            {busy === 'exit-guard' ? '김안전이 검사 중…' : `청산 감시 실행 (${exitStatus?.positions.length ?? 0}종목)`}
          </button>
        </div>
        <p className="muted mode-qualification-note">
          다섯 모드는 같은 5만 원 L0 원장·1종목 한도·기존 보유종목 제외·15초 호가·30초 대사·5분 승인을 사용합니다. 현재 전략은 L0 실험 가능 상태이며 확대 운용 자격은 아닙니다.
        </p>
        <div className="close-mode-detail">
          <div className="close-mode-rules">
            <span><small>신호 마감</small><b>{closeAuction?.spec.clock.signal_cutoff_kst ?? '15:10'}</b></span>
            <span><small>주문 창</small><b>{closeAuction?.spec.clock.closing_auction ?? '15:20~15:27'}</b></span>
            <span><small>최대 투자</small><b>자산 50%</b></span>
            <span><small>손실 중지</small><b>-15%</b></span>
          </div>
          <button
            className="secondary-button"
            disabled={busy !== null}
            onClick={() => act('close-auction', async () => {
              const result = await runCloseAuction()
              setCloseAuction(result)
            }, '키움 공식 스냅샷으로 종가매매 OOS 자격을 다시 계산했습니다.')}
          >
            {busy === 'close-auction' ? '김데이터가 검증 중…' : '종가매매 실데이터 검증'}
          </button>
        </div>
        {closeAuction && <div className="close-mode-result">
          <div className="ops-metrics">
            <span><small>OOS 거래</small><strong>{closeAuction.validation.metrics.trade_count}</strong></span>
            <span><small>순수익</small><strong>{closeAuction.validation.metrics.net_return_pct}%</strong></span>
            <span><small>Sharpe</small><strong>{closeAuction.validation.metrics.sharpe}</strong></span>
            <span><small>최대낙폭</small><strong className="negative">{closeAuction.validation.metrics.max_drawdown_pct}%</strong></span>
          </div>
          <div className="gate-list">{closeAuction.validation.failed_gates.map((gate) => <span key={gate}>{gateLabels[gate] ?? gate}</span>)}</div>
          <p className="muted">같은 날 종가를 미리 아는 계산은 금지했습니다. OOS·종목편향·과거 투자주의 상태·낙폭 기준을 모두 통과해야 그림자 운영으로 올라갑니다.</p>
        </div>}
        <div className="opening-mode-detail">
          <div className="opening-mode-title">
            <span className="pixel-kicker">김차트 · OPENING RANGE</span>
            <h3>장초 돌파 그림자 작전실</h3>
            <p>09:00에는 사지 않습니다. 첫 10분 범위를 고정하고, +1.5R부터 고정 익절 대신 상승 고점을 따라갑니다.</p>
          </div>
          <div className="close-mode-rules">
            <span><small>관찰</small><b>{openingRange?.spec.clock.observe ?? '09:00~09:10'}</b></span>
            <span><small>진입 창</small><b>{openingRange?.spec.clock.entry ?? '09:10~10:30'}</b></span>
            <span><small>하루 상한</small><b>3회</b></span>
            <span><small>강제 청산</small><b>{openingRange?.spec.clock.force_flat ?? '11:00'}</b></span>
            <span><small>신규매수 잠금</small><b>당일 +{openingRange?.spec.risk_limits.daily_new_entry_stop_pct ?? 5}%</b></span>
            <span><small>추적 강화</small><b>당일 +{openingRange?.spec.risk_limits.daily_trail_tighten_pct ?? 10}%</b></span>
            <span><small>수익 확정</small><b>당일 +{openingRange?.spec.risk_limits.daily_hard_profit_lock_pct ?? 15}%</b></span>
            <span><small>고점 이탈</small><b>{openingRange?.spec.exit.runner_trail_pct ?? 0.8}% → {openingRange?.spec.exit.runner_tight_trail_pct ?? 0.5}%</b></span>
          </div>
          <div className="opening-actions">
            <label className="opening-archive-input">
              <span>2년 장초 아카이브</span>
              <input
                value={openingArchiveFile}
                onChange={(event) => setOpeningArchiveFile(event.target.value)}
                placeholder="예: krx-2y.intraday.json.gz"
                aria-label="장초 OOS 아카이브 파일명"
              />
            </label>
            <button
              className="primary-button"
              disabled={busy !== null || !openingArchiveFile.trim()}
              onClick={() => act('opening-oos', async () => {
                const imported = await importOpeningRangeArchive(openingArchiveFile.trim())
                const result = await runOpeningRangeReplay(imported.snapshot_id)
                setOpeningRange(result)
              }, 'SHA-256이 검증된 장초 아카이브의 봉인 OOS를 계산했습니다. R1 그림자 증거는 별도 단계입니다.')}
            >
              {busy === 'opening-oos' ? '봉인 OOS 계산 중…' : '장초 아카이브 OOS 검증'}
            </button>
            <button
              className="secondary-button"
              disabled={busy !== null}
              onClick={() => act('opening-range', async () => {
                const result = await runOpeningRangeReplay()
                setOpeningRange(result)
              }, '재현 가능한 가상 장초 세션으로 진입·부분익절·고점 추적·당일 청산을 검증했습니다. 실제 주문은 0건입니다.')}
            >
              {busy === 'opening-range' ? '김차트·김안전이 재생 중…' : '장초 그림자 세션 재생'}
            </button>
            <button
              className="secondary-button"
              disabled={busy !== null}
              onClick={() => act('opening-capture', async () => {
                const result = await captureOpeningRangeRealtime()
                setOpeningCapture(result)
              }, '키움 공개시장 원시 프레임을 읽기 전용 보관함에 저장했습니다. 주문은 전송하지 않았습니다.')}
            >
              {busy === 'opening-capture' ? '10초 동안 수집 중…' : '키움 실시간 10초 수집'}
            </button>
            {openingCapture && <small>{openingCapture.event_count}프레임 · {openingCapture.event_types.join('·') || '장외 시간'} · {openingCapture.quality_state}</small>}
          </div>
        </div>
        {openingRange && <div className="opening-mode-result">
          <div className="ops-metrics">
            <span><small>완결 거래</small><strong>{openingRange.cycle.session.round_trips}</strong></span>
            <span><small>가상 순손익</small><strong className={openingRange.cycle.session.net_pnl_krw >= 0 ? 'positive' : 'negative'}>{formatKrw(openingRange.cycle.session.net_pnl_krw)}</strong></span>
            <span><small>브로커 주문</small><strong>{openingRange.cycle.session.broker_order_count}건</strong></span>
            <span><small>11시 청산</small><strong>{openingRange.cycle.session.force_flat_verified ? '통과' : '실패'}</strong></span>
          </div>
          <div className="opening-trade-list">
            {openingRange.cycle.shadow_trades.slice(0, 20).map((trade) => <article key={`${trade.symbol}-${trade.signal_at}`}>
              <div><b>{trade.name}</b><small>{trade.symbol} · {trade.state}</small></div>
              <span>{trade.quantity ?? 0}주</span>
              <span>{trade.entry_price_krw ? `${formatKrw(trade.entry_price_krw)} → ${formatKrw(trade.exit_price_krw ?? 0)}` : '미체결 취소'}</span>
              <strong className={(trade.net_pnl_krw ?? 0) >= 0 ? 'positive' : 'negative'}>{formatKrw(trade.net_pnl_krw ?? 0)}</strong>
              <small>
                {openingExitLabels[trade.exit_reason ?? ''] ?? trade.exit_reason ?? '진입 거부'}
                {trade.runner_activated ? ` · 러너 작동${trade.partial_exits?.length ? ` · ${trade.partial_exits[0].quantity}주 부분익절` : ' · 전량 추적'}` : ''}
                {' · 실제 주문 없음'}
              </small>
            </article>)}
            {openingRange.cycle.shadow_trades.length > 20 && <small className="muted">최근 화면에는 20건만 표시합니다. 전체 결과는 불변 검증 기록에 보존됩니다.</small>}
          </div>
          <div className="gate-list">{openingRange.validation.failed_gates.slice(0, 6).map((gate) => <span key={gate}>{gateLabels[gate] ?? gate}</span>)}</div>
          <p className="muted">{openingRange.cycle.source === 'FIXTURE_OPENING_RANGE_REPRODUCIBLE' ? '이 결과는 기능 검증용 가상 세션입니다.' : '승인된 아카이브의 봉인 OOS 결과입니다.'} OOS 통과 뒤에도 실시간 그림자 60일·300결정을 별도로 통과해야 실거래를 검토합니다.</p>
        </div>}
      </section>
      <section className="data-gate panel">
        <div className="panel-heading">
          <div><span className="eyebrow">POINT-IN-TIME DATA · 김데이터</span><h2>실거래 데이터 자격 관문</h2></div>
          <b className={dataQualification?.state === 'QUALIFIED_DATA_READY' ? 'positive' : 'negative'}>
            {dataQualification?.state === 'QUALIFIED_DATA_READY' ? '자격 데이터 준비' : '공식 원본 필요'}
          </b>
        </div>
        <p className="muted">공식 공공데이터와 KIND 원본을 날짜별로 결합합니다. 현재 목록에 날짜가 없거나 구간이 비면 정상처럼 추정하지 않고 실거래 자격을 닫습니다.</p>
        {dataQualification?.daily_refresh && <div className="collection-status-card">
          <div>
            <strong>김데이터 일일 자동 갱신</strong>
            <span className={['SUCCEEDED', 'CURRENT'].includes(dataQualification.daily_refresh.state) ? 'positive' : dataQualification.daily_refresh.state === 'RUNNING' ? 'warning' : 'negative'}>
              {dataQualification.daily_refresh.state === 'RUNNING' ? `진행 중 · ${dataQualification.daily_refresh.stage}` : ['SUCCEEDED', 'CURRENT'].includes(dataQualification.daily_refresh.state) ? '최신 검증 완료' : dataQualification.daily_refresh.state === 'NOT_RUN' ? '첫 실행 대기' : '자동 재시도 대기'}
            </span>
          </div>
          <p className="muted">{dataQualification.daily_refresh.schedule_kst} · 최근 기준일 {dataQualification.daily_refresh.last_success_as_of ?? '없음'} · 주문 전송 권한 없음</p>
          {dataQualification.daily_refresh.error && <p className="negative">{dataQualification.daily_refresh.error}</p>}
          <button className="secondary-button" disabled={busy !== null || dataQualification.daily_refresh.state === 'RUNNING'} onClick={() => act('daily-market-refresh', triggerDailyMarketRefresh, '김데이터 자동 갱신을 시작했습니다. 수집·검증 뒤 PASS일 때만 반영됩니다.')}>
            {busy === 'daily-market-refresh' ? '시작 중…' : dataQualification.daily_refresh.state === 'RUNNING' ? '갱신 진행 중' : '지금 다시 갱신'}
          </button>
        </div>}
        {dataQualification?.collection && <div className="collection-status-card">
          <div><strong>김데이터 수집 현황</strong><span className={dataQualification.collection.state === 'READY_FOR_QUALIFIED_BUNDLE' ? 'positive' : 'warning'}>{dataQualification.collection.state === 'READY_FOR_QUALIFIED_BUNDLE' ? '번들 생성 준비' : '이력 보완 필요'}</span></div>
          <div className="ops-metrics">
            <span><small>가격 행</small><strong>{(dataQualification.collection.metrics.price_rows ?? 0).toLocaleString()}</strong></span>
            <span><small>보통주 종목</small><strong>{(dataQualification.collection.metrics.instrument_count ?? 0).toLocaleString()}</strong></span>
            <span><small>상장폐지 포함</small><strong>{(dataQualification.collection.metrics.historical_delisted_count ?? 0).toLocaleString()}</strong></span>
            <span><small>지정 구간</small><strong>{(dataQualification.collection.metrics.designation_interval_count ?? 0).toLocaleString()}</strong></span>
          </div>
          <div className="data-gate-checks">
            {Object.entries(dataQualification.collection.checks).map(([key, passed]) => <span className={passed ? 'is-pass' : 'is-fail'} key={key}><i>{passed ? '✓' : '!'}</i>{collectionCheckLabels[key] ?? key}</span>)}
          </div>
          {dataQualification.collection.blockers.length > 0 && <p className="muted">남은 원본: {dataQualification.collection.blockers.map((key) => collectionCheckLabels[key] ?? key).join(' · ')}</p>}
        </div>}
        <div className="data-gate-checks">
          {Object.entries(dataQualification?.checks ?? {}).map(([key, passed]) => <span className={passed ? 'is-pass' : 'is-fail'} key={key}><i>{passed ? '✓' : '!'}</i>{dataQualificationLabels[key] ?? key}</span>)}
        </div>
        <div className="data-gate-flow" aria-label="자격 데이터 준비 순서">
          <span><b>1</b>KRX 공식 CSV·Excel 저장</span><i>→</i>
          <span><b>2</b>schema 2.0 + SHA-256 정규화</span><i>→</i>
          <span><b>3</b>MoneyGun 불변 스냅샷</span>
        </div>
        <div className="data-import-row">
          <label><span>가져오기함 파일명</span><input value={qualificationFile} placeholder="예: krx-5y.qualified.json.gz" onChange={(event) => setQualificationFile(event.target.value)} /></label>
          <button className="secondary-button" disabled={busy !== null || !/^[A-Za-z0-9][A-Za-z0-9_.-]*\.qualified\.json(?:\.gz)?$/.test(qualificationFile)} onClick={() => act('qualified-import', () => importQualifiedSnapshot(qualificationFile), '상장폐지·과거 지정 상태 자격 스냅샷을 검증해 저장했습니다.')}>
            {busy === 'qualified-import' ? '원본 교차검증 중…' : '자격 번들 가져오기'}
          </button>
        </div>
        <div className="data-inbox-note"><span>폴더</span><code>{dataQualification?.inbox ?? 'data/imports'}</code><span>대기 파일 {dataQualification?.qualified_bundle_files.length ?? 0}개</span></div>
      </section>
      {message && <div className="inline-feedback" role="status">{message}</div>}
      <section className="ops-check-grid" aria-label="운영 준비 상태">
        {Object.entries(readiness?.checks ?? {}).map(([key, passed]) => (
          <article className={`panel ops-check ${passed ? 'is-pass' : 'is-fail'}`} key={key}>
            <span>{passed ? '✓' : '!'}</span><div><strong>{operationCheckLabels[key] ?? key}</strong><small>{passed ? '정상' : '확인 필요'}</small></div>
          </article>
        ))}
      </section>
      <section className="panel live-activation-center" aria-label="실거래 활성화 관문">
        <div className="panel-heading">
          <div><span className="eyebrow">LIVE ACTIVATION · 실패 폐쇄</span><h2>실거래 활성화 센터</h2></div>
          <b className={activation?.state === 'LIVE_READY' ? 'positive' : 'negative'}>
            {activation?.state === 'LIVE_READY' ? 'L0 실행 가능' : '실주문 차단'}
          </b>
        </div>
        <p className="muted">5만 원 L0는 수익성 미검증 경고를 전제로 시작할 수 있지만, 배포·키움 주문 권한·주인 승인·계좌 대사 잠금은 우회하지 않습니다. 증액은 별도 OOS 자격이 필요합니다.</p>
        <div className={`morning-verdict ${activation?.state === 'LIVE_READY' ? 'is-ready' : 'is-blocked'}`} role="status">
          <strong>내일 아침 판정 · {activation?.state === 'LIVE_READY' ? '승인형 주문 준비' : '실주문 금지'}</strong>
          <span>{activation?.state === 'LIVE_READY' ? '주문 직전 최신 호가·대사·승인 범위를 다시 검사합니다.' : `현재 실주문 가능한 모드 0/${activation?.missions.length ?? 3}개입니다. 첫 번째 미통과 관문부터 해결하세요.`}</span>
        </div>
        <div className="mode-card-grid activation-card-grid">
          {activation?.missions.map((item, index) => {
            const passed = item.steps.filter((step) => step.passed).length
            const waiting = item.steps.filter((step) => !step.passed).slice(0, 4)
            return <article className={`mode-card ${item.can_submit_live_order ? 'is-active' : ''}`} key={item.mission_id}>
              <div><span className="mode-number">0{index + 1}</span><b>{item.mission_name}</b><small>{item.strategy_id ?? '정책 없음'}</small></div>
              <p>{passed}/{item.steps.length} 관문 통과 · 남은 관문 {item.blocking_count}개</p>
              <strong className={item.can_submit_live_order ? 'positive' : 'negative'}>{item.can_submit_live_order ? (item.mode_code === 'L0_PILOT' ? 'L0 소액 실주문 가능' : 'L1 확대 실주문 가능') : item.next_step?.label ?? '확인 필요'}</strong>
              <div className="maturity-checks compact">
                {waiting.map((step) => <span className="is-wait" key={step.code}><i>·</i>{step.label}<small>{step.detail}</small></span>)}
              </div>
            </article>
          })}
        </div>
        <p className="muted">이 화면 조회로 발생한 실제 주문은 {activation?.live_orders_submitted_by_check ?? 0}건입니다. 주문용 키나 승인 토큰 값은 화면에 표시하지 않습니다.</p>
      </section>
      <section className="ops-main-grid">
        <article className="panel ops-report">
          <div className="panel-heading"><div><span className="eyebrow">OOS · CHAMPION GATE</span><h2>전략 승격 판정</h2></div><b className={validation?.promotion_eligible ? 'positive' : 'negative'}>{validation?.promotion_eligible ? '승격 검토 가능' : '실거래 차단'}</b></div>
          <div className="ops-metrics">
            <span><small>거래</small><strong>{validation?.metrics.trade_count ?? 0}</strong></span>
            <span><small>순수익</small><strong>{validation?.metrics.net_return_pct ?? 0}%</strong></span>
            <span><small>Sharpe</small><strong>{validation?.metrics.sharpe ?? 0}</strong></span>
            <span><small>최대낙폭</small><strong className="negative">{validation?.metrics.max_drawdown_pct ?? 0}%</strong></span>
          </div>
          <div className="gate-list">{validation?.failed_gates.map((gate) => <span key={gate}>{gateLabels[gate] ?? gate}</span>)}</div>
          <p className="muted">수익률이 양수여도 거래 수·위험조정수익·낙폭·시점 정합성 중 하나라도 부족하면 승격하지 않습니다.</p>
        </article>
        <article className="panel ops-recovery">
          <span className="eyebrow">BACKUP · RECOVERY · DRILLS</span><h2>복구 훈련실</h2>
          <div className="status-list">
            <div><span>감사 이벤트</span><strong>{readiness?.integrity.audit_event_count ?? 0}건</strong></div>
            <div><span>해시·원장</span><strong className={readiness?.integrity.state === 'PASS' ? 'positive' : 'negative'}>{readiness?.integrity.state ?? '확인 중'}</strong></div>
            <div><span>최근 복구 점검</span><strong className={readiness?.latest_recovery?.state === 'PASS' ? 'positive' : 'warning'}>{readiness?.latest_recovery?.state ?? '없음'}</strong></div>
          </div>
          <div className="stacked-actions">
            <button className="secondary-button" disabled={busy !== null} onClick={() => act('backup', createOperationsBackup, '온라인 백업과 무결성 검사를 완료했습니다.')}>{busy === 'backup' ? '백업 중…' : '안전 백업 만들기'}</button>
            <button className="secondary-button" disabled={busy !== null} onClick={() => act('recovery', runOperationsRecoveryDrill, '격리된 임시 DB에서 복구를 검증했습니다.')}>{busy === 'recovery' ? '복구 검증 중…' : '복구 훈련'}</button>
            <button className="secondary-button" disabled={busy !== null} onClick={() => act('drills', runOperationsFailureDrills, '6가지 장애 대응 훈련을 통과했습니다.')}>{busy === 'drills' ? '훈련 중…' : '장애 대응 6종 훈련'}</button>
          </div>
        </article>
      </section>
      <section className="maturity-grid">
        <article className="panel maturity-card autonomy-card">
          <div className="panel-heading"><div><span className="eyebrow">AUTONOMY BUILD · 김운영</span><h2>무개입 개발 완료 현황</h2></div><b className="positive">{autonomy?.software_completed ?? 0}/{autonomy?.software_total ?? 9}</b></div>
          <div className="maturity-checks">
            {Object.entries(autonomy?.software ?? {}).map(([key, passed]) => <span className={passed ? 'is-pass' : 'is-fail'} key={key}><i>{passed ? '✓' : '!'}</i>{autonomyLabels[key] ?? key}</span>)}
          </div>
          <p className="muted">프로그램이 독자적으로 만들 수 있는 안전장치는 끝까지 채웁니다. 전략 변경은 제안까지만 자동이고 적용은 항상 주인 승인 후 별도 실행입니다.</p>
        </article>
        <article className="panel maturity-card">
          <div className="panel-heading"><div><span className="eyebrow">OWNER GATES · 외부 증거</span><h2>실사용 전 남은 관문</h2></div><b className="warning">사용자 단계</b></div>
          <div className="maturity-checks compact">
            {Object.entries(autonomy?.external ?? {}).map(([key, passed]) => <span className={passed ? 'is-pass' : 'is-wait'} key={key}><i>{passed ? '✓' : '·'}</i>{externalLabels[key] ?? key}</span>)}
          </div>
          <p className="muted">시간이 지나야 생기는 실적, 유료·공식 데이터, 배포 계정, 최종 승인처럼 코드가 대신 만들 수 없는 증거입니다.</p>
        </article>
      </section>
      <section className="maturity-grid triple">
        <article className="panel maturity-card">
          <div className="panel-heading"><div><span className="eyebrow">CLOSED P&amp;L · 김감사</span><h2>성과·사후검토</h2></div><b className={performance?.state === 'EVIDENCE_READY' ? 'positive' : 'warning'}>{performance?.state === 'EVIDENCE_READY' ? '증거 기록 중' : '청산 거래 대기'}</b></div>
          <div className="mini-metrics">
            <span><small>청산 거래</small><strong>{performance?.metrics.closed_trades ?? 0}</strong></span>
            <span><small>순손익</small><strong>{(performance?.metrics.net_pnl_krw ?? 0).toLocaleString('ko-KR')}원</strong></span>
            <span><small>최근 기대값</small><strong>{(performance?.metrics.recent_20_expectancy_krw ?? 0).toLocaleString('ko-KR')}원</strong></span>
            <span><small>p95 슬리피지</small><strong>{performance?.metrics.p95_abs_slippage_bps ?? 0}bp</strong></span>
          </div>
          <p className="muted">{runtimeMonitor?.action ?? '실제 청산 결과가 생기면 비용·기대값·낙폭을 자동 감시합니다.'}</p>
        </article>
        <article className="panel maturity-card">
          <div className="panel-heading"><div><span className="eyebrow">CHAMPION / CHALLENGER</span><h2>전략 대항전</h2></div><b className={comparison?.state === 'CHAMPION_SELECTED' ? 'positive' : 'negative'}>{comparison?.state === 'CHAMPION_SELECTED' ? '후보 선발' : '모두 차단'}</b></div>
          <div className="strategy-versus">
            {comparison?.rows.map((row) => <span key={row.strategy_id}><small>{row.strategy_id.startsWith('FOCUS') ? '집중투자' : '종가매매'}</small><b>{row.metrics.net_return_pct}%</b><em>{row.promotion_eligible ? '자격 통과' : `${row.failed_gates.length}개 관문 미달`}</em></span>)}
          </div>
          <p className="muted">{comparison?.selection_rule ?? '모든 자격 관문을 먼저 통과한 전략만 성과를 비교합니다.'}</p>
        </article>
        <article className="panel maturity-card">
          <div className="panel-heading"><div><span className="eyebrow">DESKTOP LIVE · FAIL CLOSED</span><h2>내 컴퓨터 24시간 운영</h2></div><b className={['DEPLOYMENT_READY', 'DESKTOP_LIVE_ELIGIBLE'].includes(deployment?.state ?? '') ? 'positive' : 'warning'}>{desktopRecoveryPassed ? '복구 대사 완료' : deployment?.state === 'DESKTOP_LIVE_ELIGIBLE' ? '복구 대사 준비' : deployment?.state === 'DEPLOYMENT_READY' ? '관리형 구성 완료' : '실주문 잠금'}</b></div>
          <div className="deployment-checks">
            {Object.entries(deployment?.checks ?? {}).map(([key, passed]) => <span className={passed ? 'is-pass' : 'is-wait'} key={key}><i>{passed ? '✓' : '·'}</i>{deployment?.check_labels?.[key] ?? deploymentLabels[key] ?? key}</span>)}
          </div>
          <p className="muted">{desktopRecoveryPassed ? '기동 후 키움 미체결·체결·잔고와 내부 원장 대사가 완료됐습니다.' : deployment?.next_action ?? '배포 공급자와 인증·비밀관리 구성이 필요합니다.'}</p>
          {deployment?.profile === 'DESKTOP_LIVE' && <button className="secondary-button" disabled={desktopRecoveryPassed || !ownerToken || busy !== null || deployment.state !== 'DESKTOP_LIVE_ELIGIBLE'} onClick={() => act('desktop-recovery', () => recoverDesktopLive(ownerToken), '키움 미체결·체결·잔고와 내부 원장을 대사해 DESKTOP_LIVE를 ARMED로 전환했습니다.')}>{busy === 'desktop-recovery' ? '복구 대사 중…' : desktopRecoveryPassed ? '복구 대사 완료' : '기동 후 복구 대사'}</button>}
          {deployment?.profile === 'DESKTOP_LIVE' && !ownerToken && !desktopRecoveryPassed && <small className="muted">그림자 주문 화면에서 저장되지 않는 소유자 토큰을 입력해야 복구 대사를 실행할 수 있습니다.</small>}
        </article>
      </section>
      <section className="panel ops-notices">
        <div className="panel-heading"><div><span className="eyebrow">IN-APP OUTBOX</span><h2>직원 보고함</h2></div><span>{unread}건 읽지 않음</span></div>
        <div className="notice-list">
          {notifications.length === 0 && <p className="muted">아직 운영 보고가 없습니다.</p>}
          {notifications.map((notice) => <button key={notice.id} className={notice.acknowledged_at ? 'is-read' : ''} onClick={() => act(`ack-${notice.id}`, () => acknowledgeOperationsNotification(notice.id), '보고를 확인 처리했습니다.')} disabled={busy !== null}>
            <i className={`notice-${notice.severity.toLowerCase()}`} /><div><strong>{notice.title}</strong><p>{notice.body}</p></div><time>{new Date(notice.created_at).toLocaleString('ko-KR')}</time>
          </button>)}
        </div>
      </section>
    </>
  )
}

function App() {
  const [page, setPage] = useState<Page>('cockpit')
  const [halted, setHalted] = useState(false)
  const [killPending, setKillPending] = useState(false)
  const [execution, setExecution] = useState<ExecutionStatus | null>(null)
  const [ownerToken, setOwnerToken] = useState('')
  const [toast, setToast] = useState('')
  const [mission, setMission] = useState<MissionSummary>(fallbackMission)
  const [connection, setConnection] = useState<'loading' | 'connected' | 'fallback'>('loading')
  const [reloadToken, setReloadToken] = useState(0)
  const [cycle, setCycle] = useState<ResearchCycle | null>(null)
  const [cycleStatus, setCycleStatus] = useState<'loading' | 'idle' | 'running' | 'error'>('loading')
  const [validation, setValidation] = useState<ResearchValidation | null>(null)
  const [validationStatus, setValidationStatus] = useState<'loading' | 'idle' | 'running' | 'error'>('loading')
  const [program, setProgram] = useState<CommitteeProgram | null>(null)
  const [programStatus, setProgramStatus] = useState<'loading' | 'idle' | 'running' | 'error'>('loading')

  useEffect(() => {
    const controller = new AbortController()
    setConnection('loading')
    fetchPrimaryMission(controller.signal)
      .then((value) => {
        setMission(value)
        setConnection('connected')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setConnection('fallback')
      })
    return () => controller.abort()
  }, [reloadToken])

  useEffect(() => {
    const controller = new AbortController()
    fetchLatestResearchCycle(controller.signal)
      .then((value) => {
        setCycle(value)
        setCycleStatus('idle')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setCycleStatus('error')
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchExecutionStatus(controller.signal)
      .then((value) => {
        setExecution(value)
        setHalted(value.control.kill_switch_active)
      })
      .catch(() => undefined)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchLatestCommitteeProgram(controller.signal)
      .then((value) => {
        setProgram(value)
        setProgramStatus('idle')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setProgramStatus('error')
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    fetchLatestResearchValidation(controller.signal)
      .then((value) => {
        setValidation(value)
        setValidationStatus('idle')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        setValidationStatus('error')
      })
    return () => controller.abort()
  }, [])

  const currentTitle = useMemo(() => navItems.find((item) => item.id === page)?.label ?? 'MoneyGun', [page])

  const notify = (message: string) => {
    setToast(message)
    window.setTimeout(() => setToast(''), 3200)
  }

  const handleExecutionChanged = (value: ExecutionStatus) => {
    setExecution(value)
    setHalted(value.control.kill_switch_active)
  }

  const toggleKill = async () => {
    if (killPending) return
    if (halted && !ownerToken) {
      notify('재개하려면 주문 화면에서 소유자 승인 토큰을 입력하고 PASS 계좌 대사를 완료하세요.')
      setPage('orders')
      return
    }
    setKillPending(true)
    try {
      if (halted) {
        await resumeKillSwitch('소유자가 PASS 대사 후 재개', ownerToken)
        notify('킬 스위치를 해제했습니다. 단계별 잠금은 그대로 유지됩니다.')
      } else {
        await activateKillSwitch('소유자 UI 긴급 중지')
        notify('신규 주문과 자동화를 즉시 중지했습니다.')
      }
      handleExecutionChanged(await fetchExecutionStatus())
    } catch (error) {
      notify(error instanceof Error ? error.message : '킬 스위치 상태 변경에 실패했습니다.')
    } finally {
      setKillPending(false)
    }
  }

  const handleRunCycle = async () => {
    setCycleStatus('running')
    try {
      const officialSnapshot = cycle?.source === 'KIWOOM_OFFICIAL_REST' ? cycle.snapshot_id : undefined
      const result = await runResearchCycle(officialSnapshot)
      setCycle(result)
      setCycleStatus('idle')
      notify(`${result.reports.length}명의 보고서와 김투자 결정을 저장했습니다.`)
    } catch {
      setCycleStatus('error')
    }
  }

  const handleRunValidation = async () => {
    if (!cycle) return
    setValidationStatus('running')
    try {
      const result = await runResearchValidation(cycle.snapshot_id)
      setValidation(result)
      setValidationStatus('idle')
      notify(result.promotion_eligible ? 'R1 승격 검토 조건을 통과했습니다.' : `${result.failed_gates.length}개 게이트가 승격을 차단했습니다.`)
    } catch {
      setValidationStatus('error')
    }
  }

  const handleRunProgram = async () => {
    setProgramStatus('running')
    try {
      const result = await runCommitteeReplay()
      setProgram(result)
      setProgramStatus('idle')
      const latestCycle = await fetchLatestResearchCycle()
      if (latestCycle) setCycle(latestCycle)
      notify(`${result.summary.completed_days}일 위원회 패키지를 재현했습니다. 실제 운영일은 0일입니다.`)
    } catch {
      setProgramStatus('error')
    }
  }

  return (
    <div className={halted ? 'app-shell is-halted' : 'app-shell'}>
      <Sidebar page={page} setPage={setPage} stage={execution?.control.stage ?? 'R0'} automation={execution?.control.automation_enabled ?? false} />
      <div className="app-column">
        <Header
          halted={halted}
          onKill={toggleKill}
          stage={execution?.control.stage ?? mission.stage}
          pending={killPending}
          title={page === 'orders' ? '5만 원 L0 실거래 픽셀 오피스' : '10만 원 집중투자 픽셀 오피스'}
        />
        {halted && <div className="halt-banner" role="alert"><strong>MISSION HALTED</strong><span>서버 킬 스위치가 신규 주문과 자동화를 차단하고 있습니다.</span></div>}
        <main className="content" aria-label={currentTitle}>
          {page === 'cockpit' && (
            <Cockpit
              onInspect={() => setPage('committee')}
              onPage={setPage}
              mission={mission}
              connection={connection}
              onRetry={() => setReloadToken((value) => value + 1)}
              cycle={cycle}
              cycleStatus={cycleStatus}
              onRunCycle={handleRunCycle}
              validation={validation}
              validationStatus={validationStatus}
              onRunValidation={handleRunValidation}
              program={program}
              programStatus={programStatus}
              onRunProgram={handleRunProgram}
            />
          )}
          {page === 'committee' && <Committee onDecision={notify} cycle={cycle} />}
          {page === 'agents' && <AgentOffice cycle={cycle} ownerToken={ownerToken} onNotice={notify} />}
          {page === 'orders' && <Orders cycle={cycle} execution={execution} ownerToken={ownerToken} onOwnerToken={setOwnerToken} onExecutionChanged={handleExecutionChanged} />}
          {page === 'audit' && <OperationsCenter ownerToken={ownerToken} />}
        </main>
      </div>
      <MobileNav page={page} setPage={setPage} />
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  )
}

export default App
