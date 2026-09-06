export type MissionSummary = {
  id: string
  name: string
  mode_code: string
  mode_version: number
  mode_display_name: string
  stage: string
  state: string
  seed_capital_krw: number
  goal_capital_krw: number
  available_krw: number
  exposed_krw: number
  reserved_profit_krw: number
  equity_krw: number
  halt_equity_krw: number
  trading_enabled: boolean
  created_at: string
}

export type ResearchReport = {
  code: string
  name: string
  role: string
  state: 'DONE' | 'WORKING' | 'WAITING' | 'CONFLICT' | 'READY'
  summary: string
  claims: Array<{ text: string; direction: string; importance: string }>
  evidence_ids: string[]
  unknowns: string[]
  invalidation_conditions: string[]
  confidence: number
}

export type ResearchCandidate = {
  rank: number
  symbol: string
  name: string
  sector: string
  score: number
  momentum: number
  quality: number
  liquidity_risk: number
  market_regime: number
  catalyst: number
  close: number
  return_63_pct: number
  stop_distance: number
}

export type ResearchCycle = {
  id: string
  mission_id: string
  snapshot_id: string
  as_of: string
  source: string
  strategy_id: string
  status: string
  candidates: ResearchCandidate[]
  reports: ResearchReport[]
  backtest: {
    status: string
    trade_count: number
    net_return_pct: number
    max_drawdown_pct: number
    win_rate_pct: number
    sharpe: number
    oos_validated: boolean
  }
  risk: {
    result: string
    quantity: number
    entry_reference_krw: number
    invalidation_price_krw: number
    risk_budget_krw: number
    order_allowed: boolean
    reasons: string[]
  }
  decision: {
    action: string
    symbol: string
    name: string
    score: number
    quantity: number
    order_allowed: boolean
    reason: string
  }
  evidence: Array<{
    id: string
    source: string
    title: string
    published_at: string
    verified: boolean
  }>
  trading_enabled: boolean
}

export type ResearchValidation = {
  id: string
  snapshot_id: string
  strategy_id: string
  as_of: string
  source: string
  status: 'ELIGIBLE_FOR_R1_REVIEW' | 'NOT_ELIGIBLE'
  promotion_eligible: boolean
  method: string
  history: {
    trading_days: number
    start: string | null
    end: string | null
    oos_days: number
  }
  metrics: {
    trade_count: number
    net_return_pct: number
    win_rate_pct: number
    max_drawdown_pct: number
    sharpe: number
    information_ratio: number
    deflated_sharpe_probability: number
  }
  gates: Record<string, boolean>
  failed_gates: string[]
  trading_enabled: boolean
}

export type CloseAuctionSpec = {
  mode_code: 'CLOSE_AUCTION'
  display_name: '종가매매'
  strategy_id: string
  market: 'KRX'
  thesis: string
  clock: {
    signal_cutoff_kst: string
    closing_auction: string
    krx_official_auction: string
  }
  entry: {
    venue: string
    order_type: string
    reference: string
    signal_information_set: string
    forbidden: string[]
  }
  exit: { time_exit: string; risk_exit: string }
  score: Record<string, number>
  risk_limits: Record<string, number>
  cost_assumption_bps: number
  trading_enabled: false
}

export type CloseAuctionBundle = {
  spec: CloseAuctionSpec
  validation: ResearchValidation
  cycle: ResearchCycle
  trading_enabled: false
}

export type LiveCloseAuctionCandidate = {
  symbol: string
  name: string
  market: string
  score: number
  current_price_krw: number
  estimated_turnover_krw: number
  market_cap_100m_krw: number
  size_band: 'SMALL_CAP' | 'LARGE_OR_UNKNOWN'
  eligible: boolean
  reasons: string[]
}

export type LiveCloseAuctionStatus = {
  state: 'WAITING_FOR_15_05' | 'BUY_CANDIDATE' | 'HOLD' | 'SCAN_MISSED' | 'WINDOW_ENDED' | 'MARKET_CLOSED'
  automatic_broker_submission: false
  spec: {
    strategy_id: 'CLOSE-AUCTION-KR-v3-L0'
    display_name: string
    clock: { signal_window: string; closing_auction: string }
    universe: {
      price_krw: [number, number]
      small_cap_band_100m_krw: [number, number]
    }
    risk: { order_budget_krw: number; approval: string; automatic_submission: false }
  }
  cycle: null | {
    id: string
    as_of: string
    strategy_id: string
    candidates: LiveCloseAuctionCandidate[]
    decision: ResearchCycle['decision']
    coverage: {
      coverage: {
        ranking_rows: number
        affordable_rows: number
        detail_candidates: number
        market_action_excluded: number
      }
      evaluated_count: number
      small_cap_evaluated_count: number
    }
  }
}

export type CloseAuctionPilotTick = LiveCloseAuctionStatus & {
  phase: string
  action: string
  reason?: string
  intent?: LiveOrderIntent
}

export type L0ModeStatus = {
  mode_code: 'FOCUS' | 'BALANCED' | 'LONG_TERM'
  state: 'NOT_RUN' | 'BUY_CANDIDATE' | 'HOLD'
  spec: {
    display_name: string
    strategy_id: string
    scan_window_kst: [string, string]
    buy_window_kst: [string, string]
    max_hold_calendar_days: number
    qualification: string
  }
  latest_cycle: null | {
    id: string
    as_of: string
    strategy_id: string
    decision: ResearchCycle['decision']
  }
}

export type L0ModesStatus = {
  modes: L0ModeStatus[]
  automatic_broker_submission: false
}

export type OpeningRangeLiveStatus = {
  phase: string
  state: null | { phase: string; event_count?: number; last_error?: string }
  spec: { display_name: string; strategy_id: string; qualification: string }
  automatic_broker_submission: false
}

export type L0ExitStatus = {
  positions: Array<{
    symbol: string
    name: string
    quantity: number
    strategy_id: string
    invalidation_price_krw: number
  }>
  existing_broker_holdings_ignored: true
  automatic_broker_submission: false
}

export type OpeningRangeSpec = {
  mode_code: 'OPENING_RANGE'
  display_name: '장초 단타'
  strategy_id: string
  validation_protocol_version: string
  market: 'KRX'
  thesis: string
  clock: {
    premarket: string
    observe: string
    entry: string
    force_flat: string
  }
  entry: {
    order_type: 'LIMIT_ONLY'
    opening_range_width_pct: number[]
    first_10m_turnover_multiple: number
    breakout_ticks: number
    chase_ceiling_pct: number
    fill_timeout_seconds: number
    benchmark_filter: string
  }
  exit: {
    initial_stop: string
    runner_activation_r: number
    partial_take_profit: string
    runner_trail_pct: number
    runner_tighten_r: number
    runner_tight_trail_pct: number
    runner_stagnation: string
    time_stop: string
    vwap_exit: string
    force_flat: string
  }
  risk_limits: {
    max_positions: number
    max_round_trips: number
    max_trade_risk_pct: number
    daily_loss_limit_pct: number
    daily_new_entry_stop_pct: number
    daily_trail_tighten_pct: number
    daily_hard_profit_lock_pct: number
    max_consecutive_losses: number
    averaging_down: boolean
    overnight_position: boolean
  }
  cost_assumption_bps: number
  promotion_gate: string
  broker_submitted: false
  trading_enabled: false
}

export type OpeningRangeCandidate = {
  eligible: boolean
  failed: string[]
  symbol: string
  name: string
  opening_high_krw: number
  opening_low_krw: number
  range_pct: number
  gap_pct: number
  relative_turnover: number
  trigger_price_krw: number
  signal_at: string | null
}

export type OpeningRangeTrade = {
  symbol: string
  name: string
  state: string
  signal_at: string
  entry_at?: string
  exit_at?: string
  quantity?: number
  entry_price_krw?: number
  stop_price_krw?: number
  runner_activation_price_krw?: number
  runner_activated?: boolean
  runner_activated_at?: string | null
  runner_high_watermark_krw?: number | null
  runner_trail_tightened?: boolean
  partial_exits?: Array<{
    at: string
    price_krw: number
    quantity: number
    reason: string
  }>
  exit_price_krw?: number
  exit_reason?: string
  net_pnl_krw?: number
  result_r?: number
  broker_submitted: false
  trading_enabled: false
}

export type OpeningRangeBundle = {
  spec: OpeningRangeSpec
  validation: ResearchValidation
  cycle: {
    id: string
    source: string
    strategy_id: string
    candidates: OpeningRangeCandidate[]
    shadow_trades: OpeningRangeTrade[]
    session: {
      round_trips: number
      net_pnl_krw: number
      stop_reason: string
      broker_order_count: number
      force_flat_verified: boolean
    }
    decision: {
      action: string
      reason: string
      order_allowed: false
    }
    trading_enabled: false
  }
  shadow_orders: ShadowOrder[]
  broker_submitted: false
  trading_enabled: false
}

export type OpeningRangeArchiveImportResult = {
  snapshot_id: string
  source: string
  session_count: number
  history: Record<string, unknown>
  quality: { state: string; issues?: string[] }
  checksum: string
  trading_enabled: false
}

export type OpeningRangeCaptureResult = {
  archive_id: string
  event_count: number
  event_types: string[]
  quality_state: 'HOLD'
  next: string
  broker_submitted: false
  trading_enabled: false
}

export type AgentScorecard = {
  code: string
  name: string
  scored_days: number
  pending_days: number
  accuracy_pct: number
  evidence_quality_pct: number
  calibration_pct: number
  overconfidence_count: number
  overall_score: number
  source_mode: 'REPLAY'
}

export type CommitteeProgram = {
  id: string
  mission_id: string
  mode: 'REPLAY' | 'DAILY'
  source: string
  start_date: string
  end_date: string
  target_days: number
  state: 'READY' | 'RUNNING' | 'PARTIAL' | 'COMPLETE'
  summary: {
    mode: string
    source: string
    target_days: number
    completed_days: number
    failed_days: number
    missing_days: number
    evaluated_days: number
    pending_outcome_days: number
    package_continuity_pct: number
    agent_scorecards: AgentScorecard[]
    readiness: {
      replay_complete: boolean
      real_operating_days: number
      qualifies_20_day_gate: boolean
      reason: string
    }
    trading_enabled: boolean
  }
  days: Array<{
    trade_date: string
    state: string
    attempts: number
    snapshot_id: string | null
    cycle_id: string | null
    error: string | null
  }>
}

export type KiwoomStatus = {
  broker: 'KIWOOM'
  connection_mode: 'READ_ONLY'
  configured: boolean
  environment: 'mock' | 'production'
  account_alias: string
  credentials_present: boolean
  token_cached: boolean
  allowed_capabilities: string[]
  blocked_capabilities: string[]
  trading_enabled: false
  blockers: string[]
}

export type BrokerAccountSnapshot = {
  id: string
  broker: string
  environment: string
  account_alias: string
  checksum: string
  payload: Record<string, unknown>
  created_at: string
  trading_enabled: false
}

export type ShadowOrderEvent = {
  state: string
  payload: Record<string, unknown>
  occurred_at: string
}

export type ShadowOrder = {
  id: string
  mission_id: string
  cycle_id: string
  trade_date: string
  next_session_date: string
  symbol: string
  name: string
  side: 'BUY'
  quantity: number
  signal_close_krw: number
  limit_price_krw: number
  invalidation_price_krw: number
  simulation_source: string
  state: string
  events: ShadowOrderEvent[]
  fill: { quantity: number; price_krw: number; occurred_at: string } | null
  broker_submitted: false
  trading_enabled: false
  created_at: string
}

export type ExecutionGate = {
  eligible: boolean
  gates: Record<string, boolean>
  metrics: Record<string, number>
}

export type ExecutionStatus = {
  guardian: {
    component: string
    environment: 'mock' | 'production'
    configured: boolean
    trading_enabled: boolean
    release_approved: boolean
    owner_auth_configured: boolean
    capital_limit_krw: number
    l0_capital_limit_krw: number
    allowed_order_types: string[]
    forbidden: string[]
    blockers: string[]
  }
  control: {
    mission_id: string
    stage: 'R0' | 'L0' | 'R1' | 'L1' | 'L2'
    kill_switch_active: boolean
    automation_enabled: boolean
    reason: string
    updated_at: string
  }
  gates: {
    L0_TO_L1: ExecutionGate
    R1_TO_L1: ExecutionGate
    L1_TO_L2: ExecutionGate
  }
  latest_reconciliation: {
    id: string
    status: 'PASS' | 'FAIL'
    details: Record<string, unknown>
    created_at: string
  } | null
  incidents: Array<{
    id: string
    severity: string
    code: string
    detail: string
    resolved_at: string | null
    created_at: string
  }>
}

export type ActivationStep = {
  code: string
  label: string
  passed: boolean
  owner_action: boolean
  detail: string
}

export type MissionActivationReadiness = {
  mission_id: string
  mission_name: string
  mode_code: 'L0_PILOT' | 'FOCUS' | 'CLOSE_AUCTION' | 'OPENING_RANGE'
  strategy_id: string | null
  state: 'L0_SETUP_REQUIRED' | 'L0_LIVE_READY' | 'RESEARCH_ONLY' | 'SHADOW_EVIDENCE_REQUIRED' | 'OWNER_AND_DEPLOYMENT_ACTION_REQUIRED' | 'L1_LIVE_READY'
  can_submit_live_order: boolean
  steps: ActivationStep[]
  blocking_count: number
  next_step: ActivationStep | null
  gates: {
    R1_TO_L1: ExecutionGate & { requirements: { operating_days: number; decisions: number } }
    L0_TO_L1: ExecutionGate
    L1_TO_L2: ExecutionGate
  }
}

export type ActivationPortfolio = {
  state: 'LIVE_READY' | 'BLOCKED'
  missions: MissionActivationReadiness[]
  live_orders_submitted_by_check: 0
}

export type LiveOrderIntent = {
  id: string
  mission_id: string
  shadow_order_id: string
  stage: 'L0' | 'L1' | 'L2'
  source_mission_id: string
  pilot_version: string | null
  performance_qualified: boolean
  symbol: string
  name: string
  side: 'BUY' | 'SELL'
  quantity: number
  limit_price_krw: number
  order_value_krw: number
  state: string
  scope_hash: string
  precheck_blockers: string[]
  approval: { decision: string; expires_at: string } | null
  events: ShadowOrderEvent[]
  fills: Array<{ quantity: number; price_krw: number }>
  created_at: string
}

export type PilotReview = {
  id: string
  checkpoint_day: 15 | 30 | 45 | 60
  candidate_version: string
  status: 'PENDING_OWNER' | 'APPROVED' | 'REJECTED'
  report: {
    period: { start: string; end: string }
    operating: Record<string, number>
    performance: Record<string, number | null>
    automatic_apply: false
    owner_decision_required: true
    notes: string[]
  }
  created_at: string
}

export type PilotStatus = {
  mission: MissionSummary
  control: ExecutionStatus['control']
  active_version: string
  strategy_performance_qualified: false
  performance_warning: string
  limits: {
    capital_krw: number
    order_budget_krw: number
    max_positions: number
    max_daily_entries: number
    daily_loss_krw: number
    total_loss_krw: number
    automatic_deposit: false
    leverage: false
  }
  progress: {
    operating_days: number
    target_days: number
    entries_today: number
    next_review_day: number | null
  }
  risk: {
    marked_equity_krw: number
    total_pnl_krw: number
    daily_defense_pnl_krw: number
    daily_loss_reached: boolean
    total_loss_reached: boolean
    can_open_new_position: boolean
    stale_symbols: string[]
  }
  reviews: PilotReview[]
  v2_candidate_ready: boolean
  automatic_version_apply: false
  live_order_count: number
}

export type OperationalRun = {
  id: string
  mission_id: string
  trade_date: string
  run_type: string
  state: 'RUNNING' | 'COMPLETE' | 'FAILED' | 'SKIPPED'
  current_stage: string
  summary: {
    snapshot_id?: string
    validation_status?: string
    decision?: { action: string; name: string; symbol: string; reason: string }
    shadow_order_id?: string | null
    attribution?: { orders: number; filled: number; cancelled: number; pending: number }
    reason?: string
    error?: string
    broker_order_sent: false
    trading_enabled: false
  }
  started_at: string
  completed_at: string | null
}

export type OperationsNotification = {
  id: string
  severity: 'INFO' | 'WARNING' | 'CRITICAL'
  category: string
  title: string
  body: string
  acknowledged_at: string | null
  created_at: string
}

export type RecoveryRun = {
  id: string
  kind: string
  state: 'PASS' | 'FAIL'
  artifact_path: string | null
  checksum: string | null
  details: Record<string, string | number | boolean>
  created_at: string
}

export type OperationsReadiness = {
  state: 'READY_SHADOW' | 'ATTENTION'
  checks: Record<string, boolean>
  control: ExecutionStatus['control']
  latest_snapshot: {
    id: string
    as_of: string
    quality: { state: string; instrument_count: number; bar_count: number }
  } | null
  latest_validation: ResearchValidation | null
  latest_run: OperationalRun | null
  latest_recovery: RecoveryRun | null
  integrity: {
    state: 'PASS' | 'FAIL'
    audit_event_count: number
    audit_errors: string[]
    ledger_errors: string[]
  }
  stage_gates: ExecutionStatus['gates']
  trading_enabled: false
}

export type DataQualificationStatus = {
  state: 'QUALIFIED_DATA_READY' | 'DATA_REQUIRED'
  checks: Record<string, boolean>
  latest_snapshot_id: string | null
  latest_snapshot_source: string | null
  inbox: string
  qualified_bundle_files: string[]
  collection: null | {
    state: 'READY_FOR_QUALIFIED_BUNDLE' | 'INCOMPLETE' | 'INVALID'
    checks: Record<string, boolean>
    metrics: {
      price_rows?: number
      issuance_rows?: number
      instrument_count?: number
      historical_delisted_count?: number
      designation_interval_count?: number
    }
    blockers: string[]
    manifest: string
  }
  daily_refresh: {
    state: 'NOT_RUN' | 'RUNNING' | 'SUCCEEDED' | 'CURRENT' | 'FAILED' | 'INTERRUPTED'
    stage: string
    schedule_kst: string
    last_started_at: string | null
    last_finished_at: string | null
    last_success_as_of: string | null
    snapshot_id: string | null
    bundle_file?: string | null
    error: string | null
    automatic_order_submission: false
  }
  next_action: string
  trading_enabled: false
}

export async function triggerDailyMarketRefresh(): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/data-qualification/daily-refresh/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export type PerformanceReport = {
  mission_id: string
  state: 'EVIDENCE_READY' | 'AWAITING_CLOSED_TRADES'
  metrics: {
    closed_trades: number
    net_pnl_krw: number
    win_rate_pct: number
    mean_return_pct: number
    profit_factor: number | null
    max_drawdown_krw: number
    recent_20_expectancy_krw: number
    mean_abs_slippage_bps: number
    p95_abs_slippage_bps: number
  }
  by_strategy: Array<Record<string, string | number>>
  by_regime: Array<Record<string, string | number>>
  by_participant: Array<{
    code: string
    name: string
    participating_trades: number
    participating_pnl_krw: number
  }>
  pending_shadow_exits: number
  latest_postmortems: Array<{
    id: string
    symbol: string
    strategy_id: string
    net_pnl_krw: number
    return_bps: number
    postmortem: { outcome: string; exit_reason: string; review_notes: string[] }
  }>
  profit_vault: Array<{ target_multiple: number; locked_amount_krw: number }>
  attribution_note: string
  trading_enabled: false
}

export type RuntimeMonitor = {
  state: 'INSUFFICIENT_EVIDENCE' | 'PASS' | 'PAUSE_NEW_BUYS'
  checks: Record<string, boolean>
  action: string
  automatic_strategy_change: false
  trading_enabled: false
}

export type StrategyComparison = {
  state: 'CHAMPION_SELECTED' | 'KEEP_ALL_BLOCKED'
  champion_strategy_id: string | null
  rows: Array<{
    mission_id: string
    strategy_id: string
    validation_id: string
    promotion_eligible: boolean
    metrics: ResearchValidation['metrics']
    failed_gates: string[]
  }>
  selection_rule: string
  automatic_promotion: false
  trading_enabled: false
}

export type DeploymentReadiness = {
  profile?: 'DESKTOP_LIVE'
  state: 'DEPLOYMENT_READY' | 'LOCAL_ONLY' | 'DESKTOP_LIVE_ELIGIBLE' | 'DESKTOP_LIVE_SETUP_REQUIRED'
  checks: Record<string, boolean>
  check_labels?: Record<string, string>
  software_boundaries: Record<string, boolean>
  next_action: string
  trading_enabled: false
}

export type AutonomyReadiness = {
  state: 'WAITING_EXTERNAL_EVIDENCE'
  software: Record<string, boolean>
  software_completed: number
  software_total: number
  external: Record<string, boolean>
  performance: PerformanceReport
  runtime_monitor: RuntimeMonitor
  deployment: DeploymentReadiness
  pending_change_requests: number
  user_action_required_now: boolean
  trading_enabled: false
}

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'
const pilotMissionId = 'mission_l0_pilot_001'

export async function fetchPrimaryMission(signal?: AbortSignal): Promise<MissionSummary> {
  const response = await fetch(`${apiBaseUrl}/v1/missions/mission_focus_001`, { signal })
  if (!response.ok) throw new Error(`Mission API returned ${response.status}`)
  return response.json() as Promise<MissionSummary>
}

export async function fetchDataQualificationStatus(
  signal?: AbortSignal,
): Promise<DataQualificationStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/data-qualification/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<DataQualificationStatus>
}

export async function importQualifiedSnapshot(fileName: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/research/snapshots/qualified-import`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': 'qualified-import-ui-v1',
    },
    body: JSON.stringify({ file_name: fileName }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export async function fetchLatestResearchCycle(signal?: AbortSignal): Promise<ResearchCycle | null> {
  const response = await fetch(`${apiBaseUrl}/v1/research/cycles/latest`, { signal })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`Research API returned ${response.status}`)
  return response.json() as Promise<ResearchCycle>
}

export async function runResearchCycle(snapshotId?: string): Promise<ResearchCycle> {
  const response = await fetch(`${apiBaseUrl}/v1/research/cycles/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'pixel-office-p0-v1' },
    body: JSON.stringify({
      mission_id: 'mission_focus_001',
      data_source: snapshotId ? 'IMPORTED_SNAPSHOT' : 'FIXTURE_KR_REPRODUCIBLE',
      snapshot_id: snapshotId,
    }),
  })
  if (!response.ok) throw new Error(`Research cycle returned ${response.status}`)
  return response.json() as Promise<ResearchCycle>
}

export async function fetchLatestResearchValidation(
  signal?: AbortSignal,
): Promise<ResearchValidation | null> {
  const response = await fetch(`${apiBaseUrl}/v1/research/validations/latest`, { signal })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`Research validation API returned ${response.status}`)
  return response.json() as Promise<ResearchValidation>
}

export async function runResearchValidation(snapshotId?: string): Promise<ResearchValidation> {
  const response = await fetch(`${apiBaseUrl}/v1/research/validations/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'pixel-office-oos-v1' },
    body: JSON.stringify({ mission_id: 'mission_focus_001', snapshot_id: snapshotId }),
  })
  if (!response.ok) throw new Error(`Research validation returned ${response.status}`)
  return response.json() as Promise<ResearchValidation>
}

export async function fetchLatestCloseAuction(
  signal?: AbortSignal,
): Promise<CloseAuctionBundle | null> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/close-auction/latest`, { signal })
  if (response.status === 404) return null
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<CloseAuctionBundle>
}

export async function runCloseAuction(): Promise<CloseAuctionBundle> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/close-auction/run`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': 'close-auction-mode-v1',
    },
    body: JSON.stringify({ mission_id: 'mission_close_auction_001' }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<CloseAuctionBundle>
}

export async function fetchLiveCloseAuctionStatus(
  signal?: AbortSignal,
): Promise<LiveCloseAuctionStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/close-auction/live/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<LiveCloseAuctionStatus>
}

export async function runCloseAuctionPilotTick(): Promise<CloseAuctionPilotTick> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/close-auction/pilot/tick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<CloseAuctionPilotTick>
}

export async function fetchL0ModesStatus(signal?: AbortSignal): Promise<L0ModesStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/l0-modes/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<L0ModesStatus>
}

export async function runL0ModeTick(
  modeCode: 'FOCUS' | 'BALANCED' | 'LONG_TERM',
  forceScan = false,
): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/l0-modes/pilot/tick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode_code: modeCode, force_scan: forceScan }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export async function fetchOpeningRangeLiveStatus(
  signal?: AbortSignal,
): Promise<OpeningRangeLiveStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/opening-range/live/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OpeningRangeLiveStatus>
}

export async function runOpeningRangePilotTick(): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/opening-range/pilot/tick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export async function fetchL0ExitStatus(signal?: AbortSignal): Promise<L0ExitStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/l0-pilot/exits/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<L0ExitStatus>
}

export async function runL0ExitTick(): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/l0-pilot/exits/tick`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export async function fetchLatestOpeningRange(
  signal?: AbortSignal,
): Promise<OpeningRangeBundle | null> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/opening-range/latest`, { signal })
  if (response.status === 404) return null
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OpeningRangeBundle>
}

export async function importOpeningRangeArchive(
  fileName: string,
): Promise<OpeningRangeArchiveImportResult> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/opening-range/import-archive`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': 'opening-range-archive-import-v1',
    },
    body: JSON.stringify({ file_name: fileName }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OpeningRangeArchiveImportResult>
}

export async function runOpeningRangeReplay(snapshotId?: string): Promise<OpeningRangeBundle> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/opening-range/run`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': 'opening-range-shadow-v2',
    },
    body: JSON.stringify({
      mission_id: 'mission_opening_range_001',
      ...(snapshotId ? { snapshot_id: snapshotId } : {}),
    }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OpeningRangeBundle>
}

export async function captureOpeningRangeRealtime(): Promise<OpeningRangeCaptureResult> {
  const response = await fetch(`${apiBaseUrl}/v1/strategies/opening-range/realtime-capture`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      symbols: ['005930', '000660', '069500'],
      max_messages: 100,
      timeout_seconds: 10,
    }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OpeningRangeCaptureResult>
}

export async function fetchLatestCommitteeProgram(
  signal?: AbortSignal,
): Promise<CommitteeProgram | null> {
  const response = await fetch(`${apiBaseUrl}/v1/committee/programs/latest`, { signal })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`Committee program API returned ${response.status}`)
  return response.json() as Promise<CommitteeProgram>
}

export async function runCommitteeReplay(): Promise<CommitteeProgram> {
  const response = await fetch(`${apiBaseUrl}/v1/committee/programs/replay`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'pixel-office-p3-replay-v1' },
    body: JSON.stringify({
      mission_id: 'mission_focus_001',
      end_date: '2026-08-31',
      target_days: 20,
    }),
  })
  if (!response.ok) throw new Error(`Committee replay returned ${response.status}`)
  return response.json() as Promise<CommitteeProgram>
}

async function apiError(response: Response): Promise<Error> {
  try {
    const body = await response.json() as { detail?: string }
    return new Error(body.detail ?? `API 오류 ${response.status}`)
  } catch {
    return new Error(`API 오류 ${response.status}`)
  }
}

export async function fetchKiwoomStatus(signal?: AbortSignal): Promise<KiwoomStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/brokers/kiwoom/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<KiwoomStatus>
}

export async function syncKiwoomAccount(): Promise<BrokerAccountSnapshot> {
  const response = await fetch(`${apiBaseUrl}/v1/brokers/kiwoom/account-snapshots/sync`, {
    method: 'POST',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<BrokerAccountSnapshot>
}

export async function fetchShadowOrders(signal?: AbortSignal): Promise<ShadowOrder[]> {
  const response = await fetch(`${apiBaseUrl}/v1/shadow/orders`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ShadowOrder[]>
}

export async function createShadowOrder(cycleId: string): Promise<ShadowOrder> {
  const response = await fetch(`${apiBaseUrl}/v1/shadow/orders`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': `shadow-ui-${cycleId}`,
    },
    body: JSON.stringify({ mission_id: 'mission_focus_001' }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ShadowOrder>
}

export async function createOfficialShadowOrder(cycleId: string): Promise<ShadowOrder> {
  const response = await fetch(`${apiBaseUrl}/v1/shadow/orders/from-official-quote`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': `official-shadow-ui-${cycleId}`,
    },
    body: JSON.stringify({ mission_id: 'mission_focus_001' }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ShadowOrder>
}

export async function simulateShadowOrder(orderId: string): Promise<ShadowOrder> {
  const response = await fetch(`${apiBaseUrl}/v1/shadow/orders/${orderId}/simulate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario: 'AUTO' }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ShadowOrder>
}

export async function settleOfficialShadowOrder(orderId: string): Promise<ShadowOrder> {
  const response = await fetch(`${apiBaseUrl}/v1/shadow/orders/${orderId}/settle-official`, {
    method: 'POST',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ShadowOrder>
}

export async function fetchExecutionStatus(
  signal?: AbortSignal,
  missionId = pilotMissionId,
): Promise<ExecutionStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/status?mission_id=${missionId}`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ExecutionStatus>
}

export async function fetchActivationReadiness(
  signal?: AbortSignal,
): Promise<ActivationPortfolio> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/activation-readiness`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ActivationPortfolio>
}

export async function fetchLiveIntents(signal?: AbortSignal): Promise<LiveOrderIntent[]> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/intents?mission_id=${pilotMissionId}`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<LiveOrderIntent[]>
}

export async function syncOfficialQuote(symbol: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/brokers/kiwoom/quotes/${symbol}/sync`, {
    method: 'POST',
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export async function runAccountReconciliation(): Promise<Record<string, unknown>> {
  const response = await fetch(`${apiBaseUrl}/v1/reconciliations/run?mission_id=${pilotMissionId}`, { method: 'POST' })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<Record<string, unknown>>
}

export async function activateKillSwitch(reason: string): Promise<ExecutionStatus['control']> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/kill-switch/activate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mission_id: pilotMissionId, reason }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ExecutionStatus['control']>
}

export async function resumeKillSwitch(
  reason: string,
  ownerToken: string,
): Promise<ExecutionStatus['control']> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/kill-switch/resume`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Owner-Approval-Token': ownerToken,
    },
    body: JSON.stringify({ mission_id: pilotMissionId, reason }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ExecutionStatus['control']>
}

export async function transitionExecutionStage(
  targetStage: 'L0' | 'R1' | 'L1' | 'L2',
  ownerToken: string,
): Promise<ExecutionStatus['control']> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/stage/transition`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Owner-Approval-Token': ownerToken,
    },
    body: JSON.stringify({ mission_id: pilotMissionId, target_stage: targetStage, reason: `${targetStage} 사용자 승격 요청` }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ExecutionStatus['control']>
}

export async function setExecutionAutomation(
  enabled: boolean,
  ownerToken: string,
): Promise<ExecutionStatus['control']> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/automation`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Owner-Approval-Token': ownerToken,
    },
    body: JSON.stringify({
      mission_id: pilotMissionId,
      enabled,
      reason: enabled ? 'L0-AUTO-v1 소유자 위임 실주문 자동화 활성화' : '자동 주문 수동 중지',
      mandate_version: 'L0-AUTO-v1',
      confirm_l0_full_loss: enabled,
    }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ExecutionStatus['control']>
}

export async function createLiveIntent(shadowOrderId: string): Promise<LiveOrderIntent> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/intents`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': `live-intent-ui-${shadowOrderId}`,
    },
    body: JSON.stringify({ shadow_order_id: shadowOrderId, execution_mission_id: pilotMissionId }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<LiveOrderIntent>
}

export async function fetchPilotStatus(signal?: AbortSignal): Promise<PilotStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/l0-pilot/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<PilotStatus>
}

export async function fetchPilotCandidates(signal?: AbortSignal): Promise<ShadowOrder[]> {
  const response = await fetch(`${apiBaseUrl}/v1/l0-pilot/candidates`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<ShadowOrder[]>
}

export async function generatePilotReviews(ownerToken: string): Promise<PilotStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/l0-pilot/reviews/generate`, {
    method: 'POST',
    headers: { 'X-Owner-Approval-Token': ownerToken },
  })
  if (!response.ok) throw await apiError(response)
  const payload = await response.json() as { status: PilotStatus }
  return payload.status
}

export async function approveLiveIntent(
  intentId: string,
  scopeHash: string,
  ownerToken: string,
): Promise<LiveOrderIntent> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/intents/${intentId}/approval`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Owner-Approval-Token': ownerToken,
    },
    body: JSON.stringify({ decision: 'APPROVE', scope_hash: scopeHash }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<LiveOrderIntent>
}

export async function submitLiveIntent(
  intentId: string,
  ownerToken: string,
): Promise<LiveOrderIntent> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/intents/${intentId}/submit`, {
    method: 'POST',
    headers: { 'X-Owner-Approval-Token': ownerToken },
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<LiveOrderIntent>
}

export async function cancelLiveIntent(
  intentId: string,
  ownerToken: string,
): Promise<LiveOrderIntent> {
  const response = await fetch(`${apiBaseUrl}/v1/execution/intents/${intentId}/cancel`, {
    method: 'POST',
    headers: { 'X-Owner-Approval-Token': ownerToken },
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<LiveOrderIntent>
}

export async function fetchOperationsReadiness(
  signal?: AbortSignal,
): Promise<OperationsReadiness> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/readiness`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OperationsReadiness>
}

export async function fetchPerformanceReport(signal?: AbortSignal): Promise<PerformanceReport> {
  const response = await fetch(`${apiBaseUrl}/v1/performance/report`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<PerformanceReport>
}

export async function fetchRuntimeMonitor(signal?: AbortSignal): Promise<RuntimeMonitor> {
  const response = await fetch(`${apiBaseUrl}/v1/performance/runtime-monitor`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<RuntimeMonitor>
}

export async function fetchStrategyComparison(signal?: AbortSignal): Promise<StrategyComparison> {
  const response = await fetch(`${apiBaseUrl}/v1/strategy-lab/comparison`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<StrategyComparison>
}

export async function fetchDeploymentReadiness(signal?: AbortSignal): Promise<DeploymentReadiness> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/deployment-readiness`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<DeploymentReadiness>
}

export async function recoverDesktopLive(ownerToken: string): Promise<{
  state: 'ARMED'
  checks: Record<string, boolean>
  broker_orders_submitted: 0
}> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/desktop-live/recover`, {
    method: 'POST',
    headers: { 'X-Owner-Approval-Token': ownerToken },
  })
  if (!response.ok) throw await apiError(response)
  return response.json()
}

export async function fetchAutonomyReadiness(signal?: AbortSignal): Promise<AutonomyReadiness> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/autonomy-readiness`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<AutonomyReadiness>
}

export async function fetchOperationsNotifications(
  signal?: AbortSignal,
): Promise<OperationsNotification[]> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/notifications`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OperationsNotification[]>
}

export async function runDailyOperations(): Promise<OperationalRun> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/daily-run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mission_id: 'mission_focus_001', force_time_gate: false }),
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OperationalRun>
}

export async function createOperationsBackup(): Promise<RecoveryRun> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/backups`, { method: 'POST' })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<RecoveryRun>
}

export async function runOperationsRecoveryDrill(): Promise<RecoveryRun> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/recovery-drill`, { method: 'POST' })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<RecoveryRun>
}

export async function runOperationsFailureDrills(): Promise<Array<{
  id: string
  scenario: string
  state: 'PASS' | 'FAIL'
  details: Record<string, string | boolean>
}>> {
  const response = await fetch(`${apiBaseUrl}/v1/operations/failure-drills`, { method: 'POST' })
  if (!response.ok) throw await apiError(response)
  return response.json()
}

export async function acknowledgeOperationsNotification(
  notificationId: string,
): Promise<OperationsNotification> {
  const response = await fetch(
    `${apiBaseUrl}/v1/operations/notifications/${notificationId}/acknowledge`,
    { method: 'POST' },
  )
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<OperationsNotification>
}

export type NewsEvent = {
  id: string
  source: 'OPENDART_OFFICIAL'
  source_event_id: string
  symbol: string
  company_name: string
  title: string
  category: string
  severity: 'CRITICAL' | 'WARNING' | 'INFO'
  sentiment: 'NEGATIVE' | 'MIXED' | 'NEUTRAL'
  review_required: boolean
  blocks_new_buy: boolean
  published_at: string
  collected_at: string
  url: string
  acknowledged_at: string | null
  analysis: {
    classifier_version: string
    matched_terms: string[]
    rationale: string
    llm_used: false
    price_or_quantity_inferred: false
  }
}

export type NewsStatus = {
  agent: { code: 'NOVA'; name: '김뉴스'; state: string; summary: string }
  state: 'NEVER_RUN' | 'ERROR' | 'STALE' | 'READY'
  configured: boolean
  scheduler_enabled: boolean
  poll_interval_seconds: number
  active_window_kst: string
  latest_poll: null | {
    state: 'PASS' | 'FAIL'
    watched_symbols: number
    fetched_count: number
    inserted_count: number
    blocking_count: number
    error: string | null
    completed_at: string
  }
  counts: { total: number; unresolved: number; active_buy_blocks: number }
  sources: Array<{ code: string; name: string; state: 'ACTIVE' | 'NOT_CONFIGURED' }>
  events: NewsEvent[]
  automatic_broker_submission: false
}

export async function fetchNewsStatus(signal?: AbortSignal): Promise<NewsStatus> {
  const response = await fetch(`${apiBaseUrl}/v1/news/status`, { signal })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<NewsStatus>
}

export async function pollNews(): Promise<NewsStatus['latest_poll']> {
  const response = await fetch(`${apiBaseUrl}/v1/news/poll`, { method: 'POST' })
  if (!response.ok) throw await apiError(response)
  return response.json()
}

export async function acknowledgeNewsEvent(
  eventId: string,
  ownerToken: string,
): Promise<NewsEvent> {
  const response = await fetch(`${apiBaseUrl}/v1/news/events/${eventId}/acknowledge`, {
    method: 'POST',
    headers: { 'X-Owner-Approval-Token': ownerToken },
  })
  if (!response.ok) throw await apiError(response)
  return response.json() as Promise<NewsEvent>
}
