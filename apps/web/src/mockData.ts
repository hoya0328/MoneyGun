export type AgentState = 'DONE' | 'WORKING' | 'WAITING' | 'CONFLICT' | 'READY'

export type Agent = {
  code: string
  name: string
  role: string
  state: AgentState
  summary: string
  updated: string
  accent: 'lime' | 'blue' | 'amber' | 'rose' | 'violet'
  icon: string
  nickname: string
  room: string
  speech: string
}

export const agents: Agent[] = [
  {
    code: 'DATA',
    name: '김데이터',
    role: '데이터 검증',
    state: 'DONE',
    summary: '가격·거래량·DART 스냅샷 검증 완료',
    updated: '15:42',
    accent: 'blue',
    icon: '🧰',
    nickname: '꼼꼼한 픽셀',
    room: '데이터 창고',
    speech: '빈칸이 하나라도 있으면 출근 도장을 찍지 않아요.',
  },
  {
    code: 'NOVA',
    name: '김뉴스',
    role: '뉴스·거시 분석',
    state: 'DONE',
    summary: '공식 출처 6건에서 촉매 2건 분류',
    updated: '15:48',
    accent: 'violet',
    icon: '📰',
    nickname: '소식 사냥꾼',
    room: '뉴스룸',
    speech: '소문은 버리고, 원문이 있는 소식만 가져왔어요!',
  },
  {
    code: 'SERENITY',
    name: '김산업',
    role: '산업·공급망',
    state: 'WORKING',
    summary: '반도체 장비 공급망 근거 교차검증 중',
    updated: '진행 중',
    accent: 'blue',
    icon: '🗺️',
    nickname: '지도 위의 탐정',
    room: '공급망 지도실',
    speech: '공장부터 고객사까지 발자국을 따라가는 중이에요.',
  },
  {
    code: 'PULSE',
    name: '김차트',
    role: '차트·모멘텀',
    state: 'DONE',
    summary: '유동성 필터 통과 후보 3개 산출',
    updated: '15:51',
    accent: 'lime',
    icon: '📈',
    nickname: '차트 파도타기',
    room: '차트 관측소',
    speech: '추세는 강하지만, 너무 뜨거운 파도는 조심해야 해요.',
  },
  {
    code: 'BULL',
    name: '김찬성',
    role: '상승 논리',
    state: 'DONE',
    summary: '후보 A의 실적 가속과 돌파 논리 제출',
    updated: '15:56',
    accent: 'lime',
    icon: '🐂',
    nickname: '가능성 수집가',
    room: '토론 테이블',
    speech: '좋은 이유 세 가지를 찾았어요. 이제 반론을 들어볼게요.',
  },
  {
    code: 'BEAR',
    name: '김반대',
    role: '반대 논리',
    state: 'CONFLICT',
    summary: '단기 과열과 고객 집중도 위험 제기',
    updated: '15:58',
    accent: 'rose',
    icon: '🐻',
    nickname: '구멍 찾는 곰',
    room: '토론 테이블',
    speech: '잠깐! 이 계약이 늦어지면 계획이 흔들릴 수 있어요.',
  },
  {
    code: 'RISK',
    name: '김안전',
    role: '한도·거부권',
    state: 'WAITING',
    summary: 'SERENITY 보고서 완료 후 최종 판정',
    updated: '대기',
    accent: 'amber',
    icon: '🛡️',
    nickname: '빨간불 지킴이',
    room: '리스크 통제실',
    speech: '보고서가 다 모이기 전에는 문을 열어주지 않아요.',
  },
  {
    code: 'ACE',
    name: '김투자',
    role: '매수·보류 결정',
    state: 'WAITING',
    summary: '찬반 논쟁과 위험 판정 대기',
    updated: '대기',
    accent: 'lime',
    icon: '🎯',
    nickname: '마지막 선택자',
    room: '포트폴리오실',
    speech: '살 이유와 사지 않을 이유를 모두 보고 결정할게요.',
  },
  {
    code: 'OPS',
    name: '김주문',
    role: '주문·체결·대사',
    state: 'READY',
    summary: '프로토타입 모드 · 실제 주문 차단됨',
    updated: '정상',
    accent: 'amber',
    icon: '🔐',
    nickname: '금고 문지기',
    room: '주문 금고',
    speech: '지금은 프로토타입이라 실제 주문 열쇠는 잠겨 있어요.',
  },
]

export const candidates = [
  {
    rank: 1,
    symbol: 'MG-A01',
    name: '샘플 반도체 장비 A',
    score: 82,
    momentum: 35,
    catalyst: 24,
    quality: 9,
    risk: '검토 중',
    change: '+3.4%',
  },
  {
    rank: 2,
    symbol: 'MG-B07',
    name: '샘플 전력 인프라 B',
    score: 78,
    momentum: 33,
    catalyst: 21,
    quality: 10,
    risk: '통과',
    change: '+1.8%',
  },
  {
    rank: 3,
    symbol: 'MG-C12',
    name: '샘플 냉각 솔루션 C',
    score: 73,
    momentum: 31,
    catalyst: 20,
    quality: 8,
    risk: '주의',
    change: '-0.6%',
  },
]

export const ladder = [
  { label: '10만', multiple: '1×', reached: true },
  { label: '20만', multiple: '2×', reached: false },
  { label: '50만', multiple: '5×', reached: false },
  { label: '100만', multiple: '10×', reached: false },
  { label: '250만', multiple: '25×', reached: false },
  { label: '500만', multiple: '50×', reached: false },
  { label: '1,000만', multiple: '100×', reached: false },
]

export const evidence = [
  { source: 'DART', title: '샘플 단일판매·공급계약 공시', time: '14:32', verified: true },
  { source: 'NEWS', title: '산업 수요 전망 공식 인터뷰', time: '13:10', verified: true },
  { source: 'IR', title: '최근 분기 실적 발표자료', time: '어제', verified: true },
]
