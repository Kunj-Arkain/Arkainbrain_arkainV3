"""
ARKAINBRAIN — i18n Module (Phase 6)

Multi-language support for generated mini-games.
Provides translation dictionaries and injection into game HTML.

Supported languages: en, es, pt, de, fr, ja, ko, zh, hi, ar
(English, Spanish, Portuguese, German, French, Japanese, Korean,
 Chinese, Hindi, Arabic)

Usage:
    from tools.i18n import I18N, inject_i18n
    i18n = I18N("es")
    html = inject_i18n(game_html, i18n)
"""

from __future__ import annotations

import json
import re
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# Translation Dictionaries
# ═══════════════════════════════════════════════════════════════

TRANSLATIONS = {
    "en": {
        "lang_code": "en", "lang_name": "English", "dir": "ltr",
        # UI labels
        "balance": "Balance",
        "bet": "Bet",
        "bet_amount": "Bet Amount",
        "place_bet": "Place Bet",
        "cashout": "Cash Out",
        "auto_cashout": "Auto Cash Out",
        "win": "Win",
        "loss": "Loss",
        "multiplier": "Multiplier",
        "result": "Result",
        "round": "Round",
        "history": "History",
        "settings": "Settings",
        "sound": "Sound",
        "provably_fair": "Provably Fair",
        "verify": "Verify",
        "server_seed": "Server Seed",
        "client_seed": "Client Seed",
        "nonce": "Nonce",
        # Game-specific
        "crash_point": "Crash Point",
        "waiting": "Waiting...",
        "crashed": "Crashed!",
        "cashed_out": "Cashed Out",
        "roll": "Roll",
        "over": "Over",
        "under": "Under",
        "spin": "Spin",
        "reveal": "Reveal",
        "higher": "Higher",
        "lower": "Lower",
        "pick": "Pick",
        "scratch_here": "Scratch Here",
        "safe": "Safe!",
        "boom": "Boom!",
        "game_over": "Game Over",
        "new_game": "New Game",
        "total_profit": "Total Profit",
        "max_win": "Max Win",
        "chance": "Chance",
        "payout": "Payout",
        "play": "Play",
        "stop": "Stop",
        "auto_play": "Auto Play",
        "rounds_left": "Rounds Left",
        # Messages
        "insufficient_balance": "Insufficient balance",
        "bet_placed": "Bet placed!",
        "congratulations": "Congratulations!",
        "better_luck": "Better luck next time",
        "jackpot_win": "JACKPOT!",
        "session_expired": "Session expired",
    },
    "es": {
        "lang_code": "es", "lang_name": "Español", "dir": "ltr",
        "balance": "Saldo",
        "bet": "Apuesta",
        "bet_amount": "Monto de Apuesta",
        "place_bet": "Apostar",
        "cashout": "Cobrar",
        "auto_cashout": "Cobro Automático",
        "win": "Ganancia",
        "loss": "Pérdida",
        "multiplier": "Multiplicador",
        "result": "Resultado",
        "round": "Ronda",
        "history": "Historial",
        "settings": "Ajustes",
        "sound": "Sonido",
        "provably_fair": "Verificablemente Justo",
        "verify": "Verificar",
        "server_seed": "Semilla del Servidor",
        "client_seed": "Semilla del Cliente",
        "nonce": "Nonce",
        "crash_point": "Punto de Caída",
        "waiting": "Esperando...",
        "crashed": "¡Cayó!",
        "cashed_out": "Cobrado",
        "roll": "Tirar",
        "over": "Mayor",
        "under": "Menor",
        "spin": "Girar",
        "reveal": "Revelar",
        "higher": "Mayor",
        "lower": "Menor",
        "pick": "Elegir",
        "scratch_here": "Rasca Aquí",
        "safe": "¡Seguro!",
        "boom": "¡Boom!",
        "game_over": "Fin del Juego",
        "new_game": "Nuevo Juego",
        "total_profit": "Ganancia Total",
        "max_win": "Ganancia Máxima",
        "chance": "Probabilidad",
        "payout": "Pago",
        "play": "Jugar",
        "stop": "Parar",
        "auto_play": "Juego Automático",
        "rounds_left": "Rondas Restantes",
        "insufficient_balance": "Saldo insuficiente",
        "bet_placed": "¡Apuesta realizada!",
        "congratulations": "¡Felicidades!",
        "better_luck": "Mejor suerte la próxima vez",
        "jackpot_win": "¡PREMIO MAYOR!",
        "session_expired": "Sesión expirada",
    },
    "pt": {
        "lang_code": "pt", "lang_name": "Português", "dir": "ltr",
        "balance": "Saldo",
        "bet": "Aposta",
        "bet_amount": "Valor da Aposta",
        "place_bet": "Apostar",
        "cashout": "Retirar",
        "auto_cashout": "Retirada Automática",
        "win": "Ganho",
        "loss": "Perda",
        "multiplier": "Multiplicador",
        "result": "Resultado",
        "round": "Rodada",
        "history": "Histórico",
        "settings": "Configurações",
        "sound": "Som",
        "provably_fair": "Comprovadamente Justo",
        "verify": "Verificar",
        "server_seed": "Semente do Servidor",
        "client_seed": "Semente do Cliente",
        "nonce": "Nonce",
        "crash_point": "Ponto de Queda",
        "waiting": "Aguardando...",
        "crashed": "Caiu!",
        "cashed_out": "Retirado",
        "roll": "Rolar",
        "over": "Acima",
        "under": "Abaixo",
        "spin": "Girar",
        "reveal": "Revelar",
        "higher": "Maior",
        "lower": "Menor",
        "pick": "Escolher",
        "scratch_here": "Raspe Aqui",
        "safe": "Seguro!",
        "boom": "Boom!",
        "game_over": "Fim de Jogo",
        "new_game": "Novo Jogo",
        "total_profit": "Lucro Total",
        "max_win": "Ganho Máximo",
        "chance": "Chance",
        "payout": "Pagamento",
        "play": "Jogar",
        "stop": "Parar",
        "auto_play": "Jogo Automático",
        "rounds_left": "Rodadas Restantes",
        "insufficient_balance": "Saldo insuficiente",
        "bet_placed": "Aposta realizada!",
        "congratulations": "Parabéns!",
        "better_luck": "Mais sorte na próxima",
        "jackpot_win": "JACKPOT!",
        "session_expired": "Sessão expirada",
    },
    "de": {
        "lang_code": "de", "lang_name": "Deutsch", "dir": "ltr",
        "balance": "Guthaben",
        "bet": "Einsatz",
        "bet_amount": "Einsatzbetrag",
        "place_bet": "Setzen",
        "cashout": "Auszahlen",
        "auto_cashout": "Auto-Auszahlung",
        "win": "Gewinn",
        "loss": "Verlust",
        "multiplier": "Multiplikator",
        "result": "Ergebnis",
        "round": "Runde",
        "history": "Verlauf",
        "settings": "Einstellungen",
        "sound": "Ton",
        "provably_fair": "Nachweislich Fair",
        "verify": "Überprüfen",
        "server_seed": "Server-Seed",
        "client_seed": "Client-Seed",
        "nonce": "Nonce",
        "crash_point": "Absturzpunkt",
        "waiting": "Warten...",
        "crashed": "Abgestürzt!",
        "cashed_out": "Ausgezahlt",
        "roll": "Würfeln",
        "over": "Über",
        "under": "Unter",
        "spin": "Drehen",
        "reveal": "Aufdecken",
        "higher": "Höher",
        "lower": "Niedriger",
        "pick": "Wählen",
        "scratch_here": "Hier Kratzen",
        "safe": "Sicher!",
        "boom": "Boom!",
        "game_over": "Spiel Vorbei",
        "new_game": "Neues Spiel",
        "total_profit": "Gesamtgewinn",
        "max_win": "Maximalgewinn",
        "chance": "Wahrscheinlichkeit",
        "payout": "Auszahlung",
        "play": "Spielen",
        "stop": "Stopp",
        "auto_play": "Automatisch Spielen",
        "rounds_left": "Runden Übrig",
        "insufficient_balance": "Unzureichendes Guthaben",
        "bet_placed": "Einsatz platziert!",
        "congratulations": "Herzlichen Glückwunsch!",
        "better_luck": "Beim nächsten Mal mehr Glück",
        "jackpot_win": "JACKPOT!",
        "session_expired": "Sitzung abgelaufen",
    },
    "fr": {
        "lang_code": "fr", "lang_name": "Français", "dir": "ltr",
        "balance": "Solde",
        "bet": "Mise",
        "bet_amount": "Montant de la Mise",
        "place_bet": "Miser",
        "cashout": "Encaisser",
        "auto_cashout": "Encaissement Auto",
        "win": "Gain",
        "loss": "Perte",
        "multiplier": "Multiplicateur",
        "result": "Résultat",
        "round": "Tour",
        "history": "Historique",
        "settings": "Paramètres",
        "sound": "Son",
        "provably_fair": "Vérifiablement Équitable",
        "verify": "Vérifier",
        "crash_point": "Point de Crash",
        "waiting": "En attente...",
        "crashed": "Crash!",
        "cashed_out": "Encaissé",
        "roll": "Lancer",
        "over": "Dessus",
        "under": "Dessous",
        "spin": "Tourner",
        "reveal": "Révéler",
        "higher": "Plus haut",
        "lower": "Plus bas",
        "pick": "Choisir",
        "scratch_here": "Grattez Ici",
        "safe": "Sûr!",
        "boom": "Boom!",
        "game_over": "Fin de Partie",
        "new_game": "Nouvelle Partie",
        "total_profit": "Profit Total",
        "max_win": "Gain Maximum",
        "chance": "Chance",
        "payout": "Paiement",
        "play": "Jouer",
        "stop": "Arrêter",
        "auto_play": "Jeu Automatique",
        "rounds_left": "Tours Restants",
        "insufficient_balance": "Solde insuffisant",
        "bet_placed": "Mise placée!",
        "congratulations": "Félicitations!",
        "better_luck": "Plus de chance la prochaine fois",
        "jackpot_win": "JACKPOT!",
        "session_expired": "Session expirée",
        "server_seed": "Graine Serveur",
        "client_seed": "Graine Client",
        "nonce": "Nonce",
    },
    "ja": {
        "lang_code": "ja", "lang_name": "日本語", "dir": "ltr",
        "balance": "残高",
        "bet": "ベット",
        "bet_amount": "ベット額",
        "place_bet": "ベットする",
        "cashout": "キャッシュアウト",
        "auto_cashout": "自動キャッシュアウト",
        "win": "勝ち",
        "loss": "負け",
        "multiplier": "倍率",
        "result": "結果",
        "round": "ラウンド",
        "history": "履歴",
        "settings": "設定",
        "sound": "サウンド",
        "provably_fair": "証明可能な公平性",
        "verify": "検証",
        "crash_point": "クラッシュポイント",
        "waiting": "待機中...",
        "crashed": "クラッシュ！",
        "cashed_out": "キャッシュアウト済",
        "roll": "ロール",
        "spin": "スピン",
        "reveal": "公開",
        "higher": "高い",
        "lower": "低い",
        "pick": "選択",
        "game_over": "ゲームオーバー",
        "new_game": "新しいゲーム",
        "play": "プレイ",
        "stop": "ストップ",
        "congratulations": "おめでとう！",
        "jackpot_win": "ジャックポット！",
        "server_seed": "サーバーシード",
        "client_seed": "クライアントシード",
        "nonce": "ナンス",
        "over": "以上", "under": "以下",
        "safe": "セーフ！", "boom": "ブーム！",
        "scratch_here": "ここをスクラッチ",
        "total_profit": "総利益", "max_win": "最大勝利",
        "chance": "確率", "payout": "配当",
        "auto_play": "オートプレイ", "rounds_left": "残りラウンド",
        "insufficient_balance": "残高不足",
        "bet_placed": "ベット完了！",
        "better_luck": "次は頑張ろう",
        "session_expired": "セッション期限切れ",
    },
    "ko": {
        "lang_code": "ko", "lang_name": "한국어", "dir": "ltr",
        "balance": "잔액", "bet": "베팅", "bet_amount": "베팅 금액",
        "place_bet": "베팅하기", "cashout": "캐시아웃",
        "win": "승리", "loss": "패배", "multiplier": "배수",
        "result": "결과", "round": "라운드", "history": "기록",
        "settings": "설정", "sound": "소리",
        "provably_fair": "공정성 증명",
        "crash_point": "크래시 포인트",
        "waiting": "대기 중...", "crashed": "크래시!",
        "spin": "스핀", "reveal": "공개",
        "higher": "높음", "lower": "낮음",
        "game_over": "게임 오버", "new_game": "새 게임",
        "play": "플레이", "stop": "정지",
        "congratulations": "축하합니다!",
        "jackpot_win": "잭팟!",
        "insufficient_balance": "잔액 부족",
        "server_seed": "서버 시드", "client_seed": "클라이언트 시드",
        "nonce": "논스", "verify": "검증",
        "auto_cashout": "자동 캐시아웃", "cashed_out": "캐시아웃 완료",
        "roll": "굴리기", "over": "이상", "under": "이하",
        "pick": "선택", "scratch_here": "여기를 긁으세요",
        "safe": "안전!", "boom": "펑!",
        "total_profit": "총 수익", "max_win": "최대 당첨",
        "chance": "확률", "payout": "지급액",
        "auto_play": "자동 플레이", "rounds_left": "남은 라운드",
        "bet_placed": "베팅 완료!",
        "better_luck": "다음에 행운을 빕니다",
        "session_expired": "세션 만료",
    },
    "zh": {
        "lang_code": "zh", "lang_name": "中文", "dir": "ltr",
        "balance": "余额", "bet": "下注", "bet_amount": "下注金额",
        "place_bet": "确认下注", "cashout": "提现",
        "win": "赢", "loss": "输", "multiplier": "倍数",
        "result": "结果", "round": "轮次", "history": "历史记录",
        "settings": "设置", "sound": "声音",
        "provably_fair": "可验证公平",
        "crash_point": "崩溃点",
        "waiting": "等待中...", "crashed": "崩溃！",
        "spin": "旋转", "reveal": "揭示",
        "higher": "更高", "lower": "更低",
        "game_over": "游戏结束", "new_game": "新游戏",
        "play": "开始", "stop": "停止",
        "congratulations": "恭喜！",
        "jackpot_win": "大奖！",
        "insufficient_balance": "余额不足",
        "server_seed": "服务器种子", "client_seed": "客户端种子",
        "nonce": "Nonce", "verify": "验证",
        "auto_cashout": "自动提现", "cashed_out": "已提现",
        "roll": "投掷", "over": "大于", "under": "小于",
        "pick": "选择", "scratch_here": "刮这里",
        "safe": "安全！", "boom": "爆炸！",
        "total_profit": "总利润", "max_win": "最高奖金",
        "chance": "概率", "payout": "赔付",
        "auto_play": "自动游戏", "rounds_left": "剩余轮次",
        "bet_placed": "已下注！",
        "better_luck": "下次好运",
        "session_expired": "会话已过期",
    },
    "hi": {
        "lang_code": "hi", "lang_name": "हिन्दी", "dir": "ltr",
        "balance": "शेष", "bet": "दांव", "bet_amount": "दांव राशि",
        "place_bet": "दांव लगाएं", "cashout": "नकद निकालें",
        "win": "जीत", "loss": "हार", "multiplier": "गुणक",
        "result": "परिणाम", "round": "राउंड", "history": "इतिहास",
        "settings": "सेटिंग्स", "sound": "ध्वनि",
        "provably_fair": "सिद्ध निष्पक्ष",
        "crash_point": "क्रैश पॉइंट",
        "waiting": "प्रतीक्षा...", "crashed": "क्रैश!",
        "spin": "घुमाएं", "reveal": "प्रकट करें",
        "higher": "ऊपर", "lower": "नीचे",
        "game_over": "गेम ओवर", "new_game": "नया गेम",
        "play": "खेलें", "stop": "रुकें",
        "congratulations": "बधाई!",
        "jackpot_win": "जैकपॉट!",
        "insufficient_balance": "अपर्याप्त शेष",
        "server_seed": "सर्वर सीड", "client_seed": "क्लाइंट सीड",
        "nonce": "नॉन्स", "verify": "सत्यापित करें",
        "auto_cashout": "ऑटो कैशआउट", "cashed_out": "कैशआउट हुआ",
        "roll": "रोल", "over": "ऊपर", "under": "नीचे",
        "pick": "चुनें", "scratch_here": "यहाँ खरोंचें",
        "safe": "सुरक्षित!", "boom": "धमाका!",
        "total_profit": "कुल लाभ", "max_win": "अधिकतम जीत",
        "chance": "संभावना", "payout": "भुगतान",
        "auto_play": "ऑटो प्ले", "rounds_left": "शेष राउंड",
        "bet_placed": "दांव लगा!",
        "better_luck": "अगली बार बेहतर भाग्य",
        "session_expired": "सत्र समाप्त",
    },
    "ar": {
        "lang_code": "ar", "lang_name": "العربية", "dir": "rtl",
        "balance": "الرصيد", "bet": "رهان", "bet_amount": "مبلغ الرهان",
        "place_bet": "ضع الرهان", "cashout": "سحب",
        "win": "فوز", "loss": "خسارة", "multiplier": "المضاعف",
        "result": "النتيجة", "round": "جولة", "history": "السجل",
        "settings": "الإعدادات", "sound": "الصوت",
        "provably_fair": "عادل بشكل مثبت",
        "crash_point": "نقطة الانهيار",
        "waiting": "انتظار...", "crashed": "انهار!",
        "spin": "دوران", "reveal": "كشف",
        "higher": "أعلى", "lower": "أدنى",
        "game_over": "انتهت اللعبة", "new_game": "لعبة جديدة",
        "play": "العب", "stop": "توقف",
        "congratulations": "تهانينا!",
        "jackpot_win": "الجائزة الكبرى!",
        "insufficient_balance": "رصيد غير كافٍ",
        "server_seed": "بذرة الخادم", "client_seed": "بذرة العميل",
        "nonce": "نونس", "verify": "تحقق",
        "auto_cashout": "سحب تلقائي", "cashed_out": "تم السحب",
        "roll": "رمي", "over": "فوق", "under": "تحت",
        "pick": "اختر", "scratch_here": "اخدش هنا",
        "safe": "آمن!", "boom": "انفجار!",
        "total_profit": "إجمالي الربح", "max_win": "أقصى ربح",
        "chance": "فرصة", "payout": "دفعة",
        "auto_play": "لعب تلقائي", "rounds_left": "جولات متبقية",
        "bet_placed": "تم الرهان!",
        "better_luck": "حظ أفضل في المرة القادمة",
        "session_expired": "انتهت الجلسة",
    },
}

SUPPORTED_LANGUAGES = list(TRANSLATIONS.keys())


# ═══════════════════════════════════════════════════════════════
# I18N Class
# ═══════════════════════════════════════════════════════════════

class I18N:
    """Translation helper."""

    def __init__(self, lang: str = "en"):
        self.lang = lang if lang in TRANSLATIONS else "en"
        self.strings = TRANSLATIONS[self.lang]
        self.fallback = TRANSLATIONS["en"]

    def t(self, key: str) -> str:
        """Translate a key, fallback to English."""
        return self.strings.get(key, self.fallback.get(key, key))

    @property
    def direction(self) -> str:
        return self.strings.get("dir", "ltr")

    @property
    def lang_name(self) -> str:
        return self.strings.get("lang_name", "English")

    def to_js_object(self) -> str:
        """Export as a JS object literal for embedding."""
        return json.dumps(self.strings, ensure_ascii=False)

    def to_dict(self) -> dict:
        return dict(self.strings)


# ═══════════════════════════════════════════════════════════════
# HTML Injection
# ═══════════════════════════════════════════════════════════════

def inject_i18n(html: str, i18n: I18N) -> str:
    """Inject i18n translations into game HTML.

    Adds:
    1. window.I18N = {...} translation object
    2. i18n(key) helper function
    3. html lang + dir attributes
    4. RTL stylesheet if needed
    """
    js_block = f"""<script>
window.I18N = {i18n.to_js_object()};
window.i18n = function(key) {{ return (window.I18N && window.I18N[key]) || key; }};
</script>"""

    # Set html lang and dir
    html = re.sub(
        r'<html[^>]*>',
        f'<html lang="{i18n.lang}" dir="{i18n.direction}">',
        html, count=1,
    )

    # RTL support
    if i18n.direction == "rtl":
        rtl_css = """<style>
[dir="rtl"] .balance-bar, [dir="rtl"] .bet-controls,
[dir="rtl"] .game-footer { direction: rtl; }
[dir="rtl"] .history-row { flex-direction: row-reverse; }
</style>"""
        js_block = rtl_css + "\n" + js_block

    # Inject before first <script>
    idx = html.find("<script>")
    if idx != -1:
        html = html[:idx] + js_block + "\n" + html[idx:]
    else:
        # inject before </head>
        idx = html.lower().find("</head>")
        if idx != -1:
            html = html[:idx] + js_block + "\n" + html[idx:]

    return html


def build_language_selector_js() -> str:
    """Generate JS for a language selector dropdown."""
    options = []
    for code, data in TRANSLATIONS.items():
        options.append(f'{{code:"{code}",name:"{data["lang_name"]}"}}')
    return f"const AVAILABLE_LANGUAGES = [{','.join(options)}];"
