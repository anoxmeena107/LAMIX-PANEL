import requests
import time
import json
import os
import uuid
import threading
import random
import re
import html
import pyotp
from collections import Counter 
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from datetime import datetime 
from urllib.parse import urljoin
from flask import Flask, Response

# Portable local database.  The complete database is kept in one JSON file,
# so it can be copied to another host and restored without an external service.
DATA_FILE = os.environ.get("BOT_DATA_FILE", "bot_data.json")
_local_db_lock = threading.RLock()

class _Increment:
    def __init__(self, amount):
        self.amount = amount

class _ServerTimestamp:
    pass

class _Snapshot:
    def __init__(self, doc_id, data=None):
        self.id = str(doc_id)
        self._data = data
        self.exists = data is not None
    def to_dict(self):
        return dict(self._data or {})

class _Document:
    def __init__(self, store, collection, doc_id):
        self.store, self.collection, self.doc_id = store, collection, str(doc_id)
    def get(self):
        with _local_db_lock:
            return _Snapshot(self.doc_id, self.store.get(self.collection, {}).get(self.doc_id))
    def set(self, data, merge=False):
        with _local_db_lock:
            bucket = self.store.setdefault(self.collection, {})
            current = bucket.get(self.doc_id, {}) if merge else {}
            bucket[self.doc_id] = _merge_local(current, data)
            _write_local_db()
    def update(self, data):
        return self.set(data, merge=True)

class _Query:
    def __init__(self, store, collection, items=None):
        self.store, self.collection = store, collection
        self.items = items
    def where(self, field, op, value):
        rows = self._rows()
        if op == ">" : rows = [(i, d) for i, d in rows if d.get(field, 0) > value]
        elif op == "==" : rows = [(i, d) for i, d in rows if d.get(field) == value]
        return _Query(self.store, self.collection, rows)
    def order_by(self, field, direction="ASCENDING"):
        rows = self._rows()
        rows.sort(key=lambda x: x[1].get(field, 0) or 0, reverse=direction == "DESCENDING")
        return _Query(self.store, self.collection, rows)
    def limit(self, count):
        return _Query(self.store, self.collection, self._rows()[:count])
    def _rows(self):
        if self.items is not None: return list(self.items)
        return list(self.store.get(self.collection, {}).items())
    def stream(self):
        return [_Snapshot(i, d) for i, d in self._rows()]
    def select(self, _fields):
        return self

class _Collection(_Query):
    def document(self, doc_id):
        return _Document(self.store, self.collection, doc_id)

class _Batch:
    def __init__(self, store):
        self.store, self.operations = store, []
    def update(self, doc, data):
        self.operations.append((doc, data))
    def commit(self):
        for doc, data in self.operations: doc.update(data)

def _merge_local(current, data):
    result = dict(current or {})
    for key, value in (data or {}).items():
        if isinstance(value, _Increment):
            result[key] = result.get(key, 0) + value.amount
        elif isinstance(value, _ServerTimestamp):
            result[key] = datetime.now().isoformat()
        else:
            result[key] = value
    return result

def _write_local_db():
    directory = os.path.dirname(os.path.abspath(DATA_FILE))
    os.makedirs(directory, exist_ok=True)
    temp_file = DATA_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8") as fp:
        json.dump(local_db_store, fp, ensure_ascii=False, indent=2)
    os.replace(temp_file, DATA_FILE)

def _load_local_db():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        print(f"⚠️ Could not read {DATA_FILE}: {exc}")
        return {}

local_db_store = _load_local_db()
DATA_FILE_EXISTED = os.path.isfile(DATA_FILE)

class _LocalDB:
    def collection(self, name):
        return _Collection(local_db_store, name)
    def batch(self):
        return _Batch(local_db_store)
    def export_bytes(self):
        with _local_db_lock:
            return json.dumps(local_db_store, ensure_ascii=False, indent=2).encode("utf-8")
    def import_bytes(self, raw):
        parsed = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        if not isinstance(parsed, dict):
            raise ValueError("Backup must contain a JSON object")
        with _local_db_lock:
            local_db_store.clear()
            local_db_store.update(parsed)
            _write_local_db()

db = _LocalDB()
# These two small helpers preserve the old counter/timestamp call sites while
# using only the local JSON database implementation above.
local_ops = type("LocalOperations", (), {
    "Increment": _Increment,
    "SERVER_TIMESTAMP": _ServerTimestamp()
})

# Config
TOKEN = "8735159035:AAF8j-ke6eku4VOvSyTpefSTsufGhlakqNk".strip()
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN not set!")

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{TOKEN}/"

OWNER_ID = 8280457872
BOT_USERNAME = ""

# ==========================================
# Render Health-Check (Flask + Waitress)
# 🌟 The mini-app system has been fully removed - this Flask app is kept only
# to bind $PORT so Render's health check considers the service alive.
# ==========================================
health_check_web = Flask(__name__)

@health_check_web.route("/")
@health_check_web.route("/healthz")
@health_check_web.route("/api/healthz")
def _health_check():
    return Response("OK", mimetype="text/plain")

def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        from waitress import serve
        serve(health_check_web, host="0.0.0.0", port=port)
    except ImportError:
        health_check_web.run(host="0.0.0.0", port=port)

# ==========================================
# Country metadata (ISO -> name, calling code) + Premium Emoji Database
# ==========================================

COUNTRY_META = {
    "AC": ("Ascension Island", "247"), "AD": ("Andorra", "376"), "AE": ("United Arab Emirates", "971"),
    "AF": ("Afghanistan", "93"), "AG": ("Antigua and Barbuda", "1"), "AI": ("Anguilla", "1"),
    "AL": ("Albania", "355"), "AM": ("Armenia", "374"), "AO": ("Angola", "244"),
    "AR": ("Argentina", "54"), "AS": ("American Samoa", "1"), "AT": ("Austria", "43"),
    "AU": ("Australia", "61"), "AW": ("Aruba", "297"), "AX": ("Aland Islands", "358"),
    "AZ": ("Azerbaijan", "994"), "BA": ("Bosnia and Herzegovina", "387"), "BB": ("Barbados", "1"),
    "BD": ("Bangladesh", "880"), "BE": ("Belgium", "32"), "BF": ("Burkina Faso", "226"),
    "BG": ("Bulgaria", "359"), "BH": ("Bahrain", "973"), "BI": ("Burundi", "257"),
    "BJ": ("Benin", "229"), "BL": ("Saint Barthélemy", "590"), "BM": ("Bermuda", "1"),
    "BN": ("Brunei", "673"), "BO": ("Bolivia", "591"), "BQ": ("Caribbean Netherlands", "599"),
    "BR": ("Brazil", "55"), "BS": ("Bahamas", "1"), "BT": ("Bhutan", "975"),
    "BW": ("Botswana", "267"), "BY": ("Belarus", "375"), "BZ": ("Belize", "501"),
    "CA": ("Canada", "1"), "CC": ("Cocos Islands", "61"), "CD": ("DR Congo", "243"),
    "CF": ("Central African Republic", "236"), "CG": ("Congo", "242"), "CH": ("Switzerland", "41"),
    "CI": ("Cote d'Ivoire", "225"), "CK": ("Cook Islands", "682"), "CL": ("Chile", "56"),
    "CM": ("Cameroon", "237"), "CN": ("China", "86"), "CO": ("Colombia", "57"),
    "CR": ("Costa Rica", "506"), "CU": ("Cuba", "53"), "CV": ("Cabo Verde", "238"),
    "CW": ("Curaçao", "599"), "CX": ("Christmas Island", "61"), "CY": ("Cyprus", "357"),
    "CZ": ("Czechia", "420"), "DE": ("Germany", "49"), "DJ": ("Djibouti", "253"),
    "DK": ("Denmark", "45"), "DM": ("Dominica", "1"), "DO": ("Dominican Republic", "1"),
    "DZ": ("Algeria", "213"), "EC": ("Ecuador", "593"), "EE": ("Estonia", "372"),
    "EG": ("Egypt", "20"), "EH": ("Western Sahara", "212"), "ER": ("Eritrea", "291"),
    "ES": ("Spain", "34"), "ET": ("Ethiopia", "251"), "FI": ("Finland", "358"),
    "FJ": ("Fiji", "679"), "FK": ("Falkland Islands", "500"), "FM": ("Micronesia", "691"),
    "FO": ("Faroe Islands", "298"), "FR": ("France", "33"), "GA": ("Gabon", "241"),
    "GB": ("United Kingdom", "44"), "GD": ("Grenada", "1"), "GE": ("Georgia", "995"),
    "GF": ("French Guiana", "594"), "GG": ("Guernsey", "44"), "GH": ("Ghana", "233"),
    "GI": ("Gibraltar", "350"), "GL": ("Greenland", "299"), "GM": ("Gambia", "220"),
    "GN": ("Guinea", "224"), "GP": ("Guadeloupe", "590"), "GQ": ("Equatorial Guinea", "240"),
    "GR": ("Greece", "30"), "GT": ("Guatemala", "502"), "GU": ("Guam", "1"),
    "GW": ("Guinea-Bissau", "245"), "GY": ("Guyana", "592"), "HK": ("Hong Kong", "852"),
    "HN": ("Honduras", "504"), "HR": ("Croatia", "385"), "HT": ("Haiti", "509"),
    "HU": ("Hungary", "36"), "ID": ("Indonesia", "62"), "IE": ("Ireland", "353"),
    "IL": ("Israel", "972"), "IM": ("Isle of Man", "44"), "IN": ("India", "91"),
    "IO": ("British Indian Ocean Territory", "246"), "IQ": ("Iraq", "964"), "IR": ("Iran", "98"),
    "IS": ("Iceland", "354"), "IT": ("Italy", "39"), "JE": ("Jersey", "44"),
    "JM": ("Jamaica", "1"), "JO": ("Jordan", "962"), "JP": ("Japan", "81"),
    "KE": ("Kenya", "254"), "KG": ("Kyrgyzstan", "996"), "KH": ("Cambodia", "855"),
    "KI": ("Kiribati", "686"), "KM": ("Comoros", "269"), "KN": ("Saint Kitts and Nevis", "1"),
    "KP": ("North Korea", "850"), "KR": ("South Korea", "82"), "KW": ("Kuwait", "965"),
    "KY": ("Cayman Islands", "1"), "KZ": ("Kazakhstan", "7"), "LA": ("Laos", "856"),
    "LB": ("Lebanon", "961"), "LC": ("Saint Lucia", "1"), "LI": ("Liechtenstein", "423"),
    "LK": ("Sri Lanka", "94"), "LR": ("Liberia", "231"), "LS": ("Lesotho", "266"),
    "LT": ("Lithuania", "370"), "LU": ("Luxembourg", "352"), "LV": ("Latvia", "371"),
    "LY": ("Libya", "218"), "MA": ("Morocco", "212"), "MC": ("Monaco", "377"),
    "MD": ("Moldova", "373"), "ME": ("Montenegro", "382"), "MF": ("Saint Martin (French part)", "590"),
    "MG": ("Madagascar", "261"), "MH": ("Marshall Islands", "692"), "MK": ("North Macedonia", "389"),
    "ML": ("Mali", "223"), "MM": ("Myanmar", "95"), "MN": ("Mongolia", "976"),
    "MO": ("Macao", "853"), "MP": ("Northern Mariana Islands", "1"), "MQ": ("Martinique", "596"),
    "MR": ("Mauritania", "222"), "MS": ("Montserrat", "1"), "MT": ("Malta", "356"),
    "MU": ("Mauritius", "230"), "MV": ("Maldives", "960"), "MW": ("Malawi", "265"),
    "MX": ("Mexico", "52"), "MY": ("Malaysia", "60"), "MZ": ("Mozambique", "258"),
    "NA": ("Namibia", "264"), "NC": ("New Caledonia", "687"), "NE": ("Niger", "227"),
    "NF": ("Norfolk Island", "672"), "NG": ("Nigeria", "234"), "NI": ("Nicaragua", "505"),
    "NL": ("Netherlands", "31"), "NO": ("Norway", "47"), "NP": ("Nepal", "977"),
    "NR": ("Nauru", "674"), "NU": ("Niue", "683"), "NZ": ("New Zealand", "64"),
    "OM": ("Oman", "968"), "PA": ("Panama", "507"), "PE": ("Peru", "51"),
    "PF": ("French Polynesia", "689"), "PG": ("Papua New Guinea", "675"), "PH": ("Philippines", "63"),
    "PK": ("Pakistan", "92"), "PL": ("Poland", "48"), "PM": ("Saint Pierre and Miquelon", "508"),
    "PR": ("Puerto Rico", "1"), "PS": ("Palestine", "970"), "PT": ("Portugal", "351"),
    "PW": ("Palau", "680"), "PY": ("Paraguay", "595"), "QA": ("Qatar", "974"),
    "RE": ("Réunion", "262"), "RO": ("Romania", "40"), "RS": ("Serbia", "381"),
    "RU": ("Russia", "7"), "RW": ("Rwanda", "250"), "SA": ("Saudi Arabia", "966"),
    "SB": ("Solomon Islands", "677"), "SC": ("Seychelles", "248"), "SD": ("Sudan", "249"),
    "SE": ("Sweden", "46"), "SG": ("Singapore", "65"), "SH": ("Saint Helena", "290"),
    "SI": ("Slovenia", "386"), "SJ": ("Svalbard and Jan Mayen", "47"), "SK": ("Slovakia", "421"),
    "SL": ("Sierra Leone", "232"), "SM": ("San Marino", "378"), "SN": ("Senegal", "221"),
    "SO": ("Somalia", "252"), "SR": ("Suriname", "597"), "SS": ("South Sudan", "211"),
    "ST": ("Sao Tome and Principe", "239"), "SV": ("El Salvador", "503"), "SX": ("Sint Maarten", "1"),
    "SY": ("Syria", "963"), "SZ": ("Eswatini", "268"), "TA": ("Tristan da Cunha", "290"),
    "TC": ("Turks and Caicos Islands", "1"), "TD": ("Chad", "235"), "TG": ("Togo", "228"),
    "TH": ("Thailand", "66"), "TJ": ("Tajikistan", "992"), "TK": ("Tokelau", "690"),
    "TL": ("Timor-Leste", "670"), "TM": ("Turkmenistan", "993"), "TN": ("Tunisia", "216"),
    "TO": ("Tonga", "676"), "TR": ("Turkey", "90"), "TT": ("Trinidad and Tobago", "1"),
    "TV": ("Tuvalu", "688"), "TW": ("Taiwan", "886"), "TZ": ("Tanzania", "255"),
    "UA": ("Ukraine", "380"), "UG": ("Uganda", "256"), "US": ("United States", "1"),
    "UY": ("Uruguay", "598"), "UZ": ("Uzbekistan", "998"), "VA": ("Vatican City", "39"),
    "VC": ("Saint Vincent and the Grenadines", "1"), "VE": ("Venezuela", "58"), "VG": ("British Virgin Islands", "1"),
    "VI": ("US Virgin Islands", "1"), "VN": ("Vietnam", "84"), "VU": ("Vanuatu", "678"),
    "WF": ("Wallis and Futuna", "681"), "WS": ("Samoa", "685"), "XK": ("Kosovo", "383"),
    "YE": ("Yemen", "967"), "YT": ("Mayotte", "262"), "ZA": ("South Africa", "27"),
    "ZM": ("Zambia", "260"), "ZW": ("Zimbabwe", "263"),
}

# ==========================================
PEM = {
    "ok": '<tg-emoji emoji-id="5352694861990501856">✅</tg-emoji>',
    "no": '<tg-emoji emoji-id="5420130255174145507">❌</tg-emoji>',
    "warn": '<tg-emoji emoji-id="5336944168944047463">⚠️</tg-emoji>',
    "admin": '<tg-emoji emoji-id="5353032893096567467">📊</tg-emoji>',
    "user": '<tg-emoji emoji-id="5352861489541714456">👤</tg-emoji>',
    "file": '<tg-emoji emoji-id="5352721946054268944">📁</tg-emoji>',
    "rocket": '<tg-emoji emoji-id="5352597830089347330">🚀</tg-emoji>',
    "graph": '<tg-emoji emoji-id="5352877703043258544">📊</tg-emoji>',
    "money": '<tg-emoji emoji-id="5348469219761626211">💸</tg-emoji>',
    "gift": '<tg-emoji emoji-id="5420396762189831222">🎁</tg-emoji>',
    "msg": '<tg-emoji emoji-id="5337302974806922068">💬</tg-emoji>',
    "gear": '<tg-emoji emoji-id="5420155432272438703">⚙️</tg-emoji>',
    "link": '<tg-emoji emoji-id="5420517437885943844">🔗</tg-emoji>',
    "trash": '<tg-emoji emoji-id="5422557736330106570">🗑</tg-emoji>',
    "upload": '<tg-emoji emoji-id="5353001161878182134">📤</tg-emoji>',
    "world": '<tg-emoji emoji-id="5336972142066047577">🌐</tg-emoji>',
    "lock": '<tg-emoji emoji-id="5353022963132174959">🔐</tg-emoji>',
    "phone": '<tg-emoji emoji-id="5337132498965010628">📱</tg-emoji>',
    "num": '<tg-emoji emoji-id="5352862640592949843">🔢</tg-emoji>',
    "pin": '<tg-emoji emoji-id="5352922460897452503">📍</tg-emoji>',
    "star": '<tg-emoji emoji-id="5352552689983067014">✨</tg-emoji>',
    "hi": '<tg-emoji emoji-id="5353027129250453493">👋</tg-emoji>',
    # 🌟 Added from NewsEmoji pack
    "gold": '<tg-emoji emoji-id="5440539497383087970">🥇</tg-emoji>',
    "silver": '<tg-emoji emoji-id="5447203607294265305">🥈</tg-emoji>',
    "bronze": '<tg-emoji emoji-id="5453902265922376865">🥉</tg-emoji>',
    "crown": '<tg-emoji emoji-id="5217822164362739968">👑</tg-emoji>',
    "celebrate": '<tg-emoji emoji-id="5461151367559141950">🎉</tg-emoji>',
    "info": '<tg-emoji emoji-id="5334544901428229844">ℹ️</tg-emoji>',
    "search": '<tg-emoji emoji-id="5231012545799666522">🔍</tg-emoji>',
    "refresh": '<tg-emoji emoji-id="5375338737028841420">🔄</tg-emoji>',
    "new": '<tg-emoji emoji-id="5382357040008021292">🆕</tg-emoji>',
    "top": '<tg-emoji emoji-id="5415655814079723871">🔝</tg-emoji>',
    "fire": '<tg-emoji emoji-id="5424972470023104089">🔥</tg-emoji>',
    "check": '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>',
    "idea": '<tg-emoji emoji-id="5422439311196834318">💡</tg-emoji>',
    "calendar": '<tg-emoji emoji-id="5413879192267805083">🗓</tg-emoji>'
}

GLOBAL_BODY_EMOJIS = {
    "➖": "5870818207383686839", "🚫": "5334807341109908955", "😒": "5334763399299506604",
    "🖥": "5334880948259427772", "🌐": "5334590977837403844", "🌟": "5337102391244263212",
    "🕓": "5336983442125001376", "⌛": "5337172996211648018", "💬": "5337302974806922068",
    "🔐": "5337255927735163754", "🍏": "5337132498965010628", "❔": "5336850036145823599",
    "⚠️": "5336944168944047463", "🔥": "5337267511261960341", "💸": "5348469219761626211",
    "🥚": "5348390922507817684", "👨‍⚖": "5334763399299506604", "🐁": "5348494358205207761",
    "🧻": "5348486915026884464", "⚗": "5346311574221000149", "🛴": "5348075478634766440",
    "📊": "5353032893096567467", "🔢": "5352862640592949843", "👤": "5352861489541714456",
    "📁": "5352721946054268944", "🚀": "5352597830089347330", "💎": "5352838545826420397",
    "📍": "5352922460897452503", "👋": "5353027129250453493", "✅": "5352694861990501856",
    "1️⃣": "5352651766288652742", "2️⃣": "5355186458418257716", "3️⃣": "5352867219028091093",
    "4️⃣": "5352566657216714037", "5️⃣": "5353086880835474989", "6️⃣": "5354859211975071385",
    "7️⃣": "5352859127309707652", "8️⃣": "5352957533600389988", "9️⃣": "5353060913463204207",
    "🔤": "5352727417842606016", "📣": "5352980533150259581", "📤": "5353001161878182134",
    "✨": "5352552689983067014", "🔹": "5352638632278660622", "🎙": "5355102594886833928",
    "💴": "5352985330628730418", "📅": "5352585194295564660", "📴": "5352974971167611327",
    "✏️": "5395444784611480792", "📱": "5337132498965010628", "🔗": "5420517437885943844",
    "❌": "5420130255174145507", "⚙️": "5420155432272438703", "🫂": "5420145051336485498",
    "➕": "5420323438508155202", "🗑": "5422557736330106570", "🎁": "5420396762189831222",
    "➤": "5420618897898381296", "🏢": "5420156334215565595", "💳": "5190899075968441286",
    "📝": "5192739271886282680", "🛡": "5190447043545438788", "🤝": "5192805934073685937",
    "💰": "5190576863226933563", "👀": "5190645917711114179", "🕹": "5193100774988617665",
    "🟢": "5192812028632274956", "🧪": "5190781475468915802", "🎨": "5190751148704833975",
    "📂": "5257969839313526622", "🌍": "5780471598922337683", "📌": "5318986077455795572",
    "📢": "5789428375261023681", "🆔": "5352862640592949843", "📈": "5352877703043258544",
    "🔔": "5352980533150259581", "🏦": "5348469219761626211", "🧾": "5192739271886282680",
    "👨‍⚖️": "5334763399299506604", "🔍": "5463352748751753567",
    "🔑": "5197288647275071607",
    # 🌟 Added from NewsEmoji pack - only new emoji chars, existing ones kept as-is
    "🙂": "5461117441612462242", "⚡️": "5456140674028019486", "☄️": "5224607267797606837",
    "🛍": "5229064374403998351", "⛔️": "5260293700088511294", "❗️": "5274099962655816924",
    "‼️": "5440660757194744323", "⁉️": "5314504236132747481", "❓": "5436113877181941026",
    "💭": "5467538555158943525", "🔼": "5449683594425410231", "🔽": "5447183459602669338",
    "🕯": "5451882707875276247", "📉": "5246762912428603768", "✔️": "5206607081334906820",
    "🆒": "5222079954421818267", "🥸": "5391112412445288650", "🤡": "5269531045165816230",
    "🫦": "5395444514028529554", "💵": "5409048419211682843", "💱": "5402186569006210455",
    "▶️": "5264919878082509254", "🔴": "5411225014148014586", "➡️": "5416117059207572332",
    "💥": "5276032951342088188", "🎤": "5224736245665511429", "🤫": "5431609822288033666",
    "👎": "5449875686837726134", "🗣️": "5460795800101594035", "©": "5323442290708985472",
    "ℹ️": "5334544901428229844", "👍": "5337080053119336309", "⏸": "5359543311897998264",
    "💯": "5341498088408234504", "🔄": "5375338737028841420", "🔝": "5415655814079723871",
    "🆕": "5382357040008021292", "🔜": "5440621591387980068", "⭐️": "5438496463044752972",
    "👑": "5217822164362739968", "🔖": "5222444124698853913", "✉️": "5253742260054409879",
    "🔒": "5296369303661067030", "😮": "5303479226882603449", "📎": "5305265301917549162",
    "🎮": "5361741454685256344", "🔈": "5388632425314140043", "⬇️": "5406745015365943482",
    "☀️": "5402477260982731644", "🌧": "5399913388845322366", "🌛": "5449569374065152798",
    "❄️": "5449449325434266744", "🌈": "5409109841538994759", "💧": "5393512611968995988",
    "🗓": "5413879192267805083", "💡": "5422439311196834318", "🥇": "5440539497383087970",
    "🥈": "5447203607294265305", "🥉": "5453902265922376865", "🎵": "5463107823946717464",
    "🆓": "5406756500108501710", "🚨": "5395695537687123235", "🏠": "5416041192905265756",
    "🚩": "5460755126761312667", "🎉": "5461151367559141950"
}

DEFAULT_CUSTOM_MESSAGES = {
    "start": {"text": "╔═══════════╗\n       📊 NUMBER BOT\n╚═══════════╝\n🚀 Welcome to Number & OTP Service\n━━━━━━━━━━━━\n✅ Choose an option below\nto continue using the bot.\n━━━━━━━━━━━━\n💎 Premium OTP Service", "buttons": []},
    "get_number": {"text": f"{PEM['pin']} Select a service:", "buttons": []},
    "select_country": {"text": f"📌 Select a country for {{service}}:", "buttons": []}, 
    "refer": {"text": f"➖➖➖➖➖➖➖\n« {PEM['gift']} REFER & EARN »\n➖➖➖➖➖➖➖\n{PEM['link']} YOUR LINK:\n<code>{{ref_link}}</code>\n➖➖➖➖➖➖➖\n{PEM['user']} TOTAL REFERS: <b>{{total_ref}}</b>\n➖➖➖➖➖➖➖\n{PEM['money']} PER REFER: <b>{{ref_reward}} TK</b>\n➖➖➖➖➖➖➖", "buttons": []},
    "withdrawal": {"text": "➖➖➖➖➖➖➖\n《 😒 WITHDRAWAL 》\n➖➖➖➖➖➖➖\n👋 Total Otp: {total_otp}\n➖➖➖➖➖➖➖\n🫂 Total Reffer :{total_ref}\n➖➖➖➖➖➖➖\n📅 BALANCE: {bal}৳\n➖➖➖➖➖➖➖\n🔐 MINIMUM: {min_w} ৳\n➖➖➖➖➖➖➖\nSELECT METHOD:", "buttons": []},
    "support": {"text": f"{PEM['msg']} Contact us for any help:", "buttons": []}
}

print(f"✅ Local database ready: {DATA_FILE}")
if not DATA_FILE_EXISTED:
    with _local_db_lock:
        _write_local_db()
    print("🆕 New data file created because no backup was found.")
else:
    print("♻️ Existing data file found; it will be used as-is.")

bot_settings = {
    "admins": [OWNER_ID],
    "panels": [], 
    "fw_groups": [], 
    "otp_link": "https://t.me/your_otp_group",
    "withdraw_on": True,
    "min_withdraw": 30.0,
    "otp_reward": 0.1,
    "refer_reward": 0.2,
    "weekly_reset_at": 0,
    "cooldown": 10,
    "num_req": 3,
    "num_share": 1, 
    "support_link": "https://t.me/your_support",
    "w_methods": ["bKash", "Nagad"],
    "w_group": "", 
    
    "fj_on": False,
    "fj_channels": [], 
    "stex_keys": [], 
    "voltx_keys": [],
    "zebrasms_keys": [],
    "yesms_keys": [],
    "stex_services": {},
    "voltx_services": {},
    "zebrasms_services": {},
    "yesms_services": {},
    "stex_service_rates": {},
    "voltx_service_rates": {},
    "zebrasms_service_rates": {},
    "yesms_service_rates": {},
    "premium_flags": {
        "1": {"char": "🇺🇸", "iso": "US", "name": "United States", "id": "5913463998522592692"},
        "880": {"char": "🇧🇩", "iso": "BD", "name": "Bangladesh", "id": "5911365056594973179"},
        "91": {"char": "🇮🇳", "iso": "IN", "name": "India", "id": "5913754823643107921"},
        "92": {"char": "🇵🇰", "iso": "PK", "name": "Pakistan", "id": "5913705895375672082"},
        "44": {"char": "🇬🇧", "iso": "GB", "name": "United Kingdom", "id": "5913443365499703513"}
    },
    "premium_apps": {
        # 🌟 Expanded from the Premium_App.txt pack the admin provided, so OTP
        # cards show a real premium icon for every common service instead of
        # the generic 📱 fallback. Keys match SERVICE_SMS_KEYWORDS where one
        # exists (so incoming SMS text auto-detects them); others are matched
        # directly against a manually configured service name.
        "FACEBOOK": {"char": "📘", "id": "5334807341109908955", "name": "Facebook"},
        "WHATSAPP": {"char": "💬", "id": "5334759662677957452", "name": "WhatsApp"},
        "TELEGRAM": {"char": "✈️", "id": "5337010556253543833", "name": "Telegram"},
        "IMO": {"char": "💭", "id": "5337155807752524558", "name": "Imo"},
        "INSTAGRAM": {"char": "📸", "id": "5334868205091459431", "name": "Instagram"},
        "APPLE": {"char": "🍎", "id": "5334637951894722661", "name": "Apple"},
        "GOOGLE": {"char": "🔍", "id": "5335010201005231986", "name": "Google"},
        "MICROSOFT": {"char": "🪟", "id": "5334880948259427772", "name": "Microsoft"},
        "TEAMS": {"char": "🧑‍🤝‍🧑", "id": "5334590977837403844", "name": "Teams"},
        "TIKTOK": {"char": "🎵", "id": "5339213256001102461", "name": "Tiktok"},
        "BKASH": {"char": "🏦", "id": "5348469219761626211", "name": "Bkash"},
        "ROCKET": {"char": "🚀", "id": "5346042941196507141", "name": "Rocket"},
        "BYBIT": {"char": "📈", "id": "5348372939479751825", "name": "Bybit"},
        "BINANCE": {"char": "💱", "id": "5348212415077064131", "name": "Binance"},
        "MELBET": {"char": "🌟", "id": "5337102391244263212", "name": "Melbet"},
        "SNAPCHAT": {"char": "👻", "id": "5359441366554255082", "name": "Snapchat"},
        "UBER": {"char": "🚗", "id": "5298715455316303708", "name": "Uber"},
        "PAYPAL": {"char": "💵", "id": "5776103539872896061", "name": "PayPal"},
        "DISCORD": {"char": "🎬", "id": "5116246243646898866", "name": "Discord"},
        "AMAZON": {"char": "🌟", "id": "4995019580536524226", "name": "Amazon"},
        "VIBER": {"char": "💜", "id": "5463060437572528782", "name": "Viber"},
        "LINKEDIN": {"char": "💼", "id": "6224222994265279792", "name": "Linkedin"},
        "LINE": {"char": "🔒", "id": "5399818044866327279", "name": "Line"},
        "WECHAT": {"char": "🌟", "id": "5782757599560602950", "name": "Wechat"},
        "TWITTER": {"char": "🐦", "id": "5215726959056662534", "name": "Twitter"},
        "REDDIT": {"char": "👽", "id": "4992421103847604984", "name": "Reddit"},
        "PINTEREST": {"char": "📌", "id": "5346103513120258857", "name": "Pinterest"},
        "TWITCH": {"char": "🎮", "id": "5233333563306301418", "name": "Twitch"},
        "ZOOM": {"char": "📹", "id": "5881799193219043268", "name": "Zoom"},
        "SIGNAL": {"char": "💬", "id": "5293998404404272267", "name": "Signal"},
        "SLACK": {"char": "💻", "id": "4994972469040251302", "name": "Slack"},
        "SKYPE": {"char": "☎️", "id": "4992613535562334989", "name": "Skype"},
        "NETFLIX": {"char": "🎥", "id": "6255738712664050133", "name": "Netflix"},
        "SPOTIFY": {"char": "🎵", "id": "5411392711146095115", "name": "Spotify"},
        "AMAZONPRIME": {"char": "📺", "id": "6111801057061374810", "name": "Amazon Prime"},
        "HOICHOI": {"char": "🍿", "id": "6104822598493801746", "name": "Hoichoi"},
        "DARAZ": {"char": "📦", "id": "5336879280578138635", "name": "Daraz"},
        "FOODPANDA": {"char": "🐼", "id": "5336879280578138635", "name": "Foodpanda"},
        "PATHAO": {"char": "🛵", "id": "5336879280578138635", "name": "Pathao"},
        "ALIEXPRESS": {"char": "🛒", "id": "5336879280578138635", "name": "AliExpress"},
        "SHOPEE": {"char": "🛍️", "id": "5336879280578138635", "name": "Shopee"},
        "PAYONEER": {"char": "💳", "id": "5336879280578138635", "name": "Payoneer"},
        "WISE": {"char": "🦉", "id": "5336879280578138635", "name": "Wise"},
        "CHATGPT": {"char": "🤖", "id": "5296516998996445955", "name": "ChatGPT"},
        "NOTION": {"char": "📓", "id": "5336879280578138635", "name": "Notion"},
        "GITHUB": {"char": "🐙", "id": "5417836094098007862", "name": "GitHub"},
        "CANVA": {"char": "🖌️", "id": "5111661409008092227", "name": "Canva"},
        "FIGMA": {"char": "🎨", "id": "5336879280578138635", "name": "Figma"},
        "UPWORK": {"char": "💼", "id": "5336879280578138635", "name": "Upwork"},
        "FIVERR": {"char": "🟢", "id": "5336879280578138635", "name": "Fiverr"},
        "YAHOO": {"char": "🌐", "id": "5336879280578138635", "name": "Yahoo"},
        "DROPBOX": {"char": "☁️", "id": "5336879280578138635", "name": "Dropbox"},
        "COURSERA": {"char": "📚", "id": "5336879280578138635", "name": "Coursera"},
        "DUOLINGO": {"char": "🗣️", "id": "5336879280578138635", "name": "Duolingo"}
    },
    "status_services": [],
    "custom_messages": DEFAULT_CUSTOM_MESSAGES.copy()
}

FS_KEYS = [
    "admins", "panels", "fw_groups", "otp_link", "withdraw_on", 
    "min_withdraw", "otp_reward", "refer_reward", "cooldown", 
    "num_req", "num_share", "support_link", "w_methods", "w_group", "stex_keys", "voltx_keys", "stex_services", "voltx_services",
    "fj_on", "fj_channels", "zebrasms_keys", "zebrasms_services", "yesms_keys", "yesms_services",
    "stex_service_rates", "voltx_service_rates", "zebrasms_service_rates", "yesms_service_rates",
    # 🌟 FIX: these were missing before, so every admin edit to premium flags/app icons and
    # custom menu text (start/get_number/refer/withdrawal/support) was silently lost on
    # every restart/redeploy - they are written to the local data file.
    "premium_flags", "premium_apps", "custom_messages", "status_services", "weekly_reset_at"
]

number_batches = {}
used_numbers_list = []
stex_assigned_numbers = {} 
voltx_assigned_numbers = {}
zebrasms_assigned_numbers = {}
yesms_assigned_numbers = {}
STEX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tness/@public/api"
VOLTX_BASE_URL = "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"
ZEBRASMS_BASE_URL = "https://zebrasms.com/api/v1"
YESMS_BASE_URL = "https://yesms.online/api"
total_uploaded_stats = 0
total_assigned_stats = 0
processed_otps = set() 
user_banned_cache = {}

# Active HTTP sessions for Auto Captcha Panels
panel_sessions = {}

# 🌟 sAjaxSource (AJAX/DataTable) এবং Fallback HTML Parser Helper Function
def fetch_cpt_panel_cdrs(p, session, check_url):
    res = session.get(check_url, timeout=15)
    html_text = res.text
    
    # সেশন শেষ হয়েছে বা লগইন পেজে রিডাইরেক্ট করেছে কি না তা চেক করা
    if "login" in html_text.lower() or "signin" in html_text.lower() or any(x in html_text for x in ["Sign in to your account", "Please sign in", "Welcome back!"]):
        raise Exception("Session expired")
        
    soup = BeautifulSoup(html_text, 'html.parser')
    s_ajax_source = ""
    for script in soup.find_all("script"):
        script_text = script.string or ""
        match = re.search(r'sAjaxSource":\s*"([^"]+)"', script_text)
        if match:
            s_ajax_source = match.group(1)
            break
            
    results = []
    
    n_col_name = p.get("num_col_name", "number").lower()
    m_col_name = p.get("msg_col_name", "message").lower()
    n_idx = int(p.get("num_col_idx", 1)) - 1 if p.get("num_col_idx") else 1
    m_idx = int(p.get("msg_col_idx", 2)) - 1 if p.get("msg_col_idx") else 2

    # ৫.১ যদি sAjaxSource AJAX লিংক পাওয়া যায়
    if s_ajax_source:
        baseUrl = p.get("login_url", "").split("/client")[0].split("/login")[0].strip()
        if not baseUrl.startswith("http"):
            baseUrl = "http://" + baseUrl
            
        full_ajax_url = ""
        if s_ajax_source.startswith("http"):
            full_ajax_url = s_ajax_source
        elif s_ajax_source.startswith("/"):
            full_ajax_url = f"{baseUrl}{s_ajax_source}"
        else:
            last_slash_idx = check_url.rfind("/")
            current_dir = check_url[:last_slash_idx]
            full_ajax_url = f"{current_dir}/{s_ajax_source}"

        if "iDisplayLength" not in full_ajax_url:
            # 250 এর জায়গায় 10000 করে দেওয়া হলো যাতে সব মেসেজ ফেচ করতে পারে
            query_params = "sEcho=1&iColumns=7&iDisplayStart=0&iDisplayLength=10000&sSearch=&iSortingCols=1&iSortCol_0=0&sSortDir_0=desc"
            divider = "&" if "?" in full_ajax_url else "?"
            full_ajax_url += f"{divider}{query_params}"

        ajax_headers = {
            "Referer": check_url,
            "X-Requested-With": "XMLHttpRequest"
        }
        
        ajax_res = session.get(full_ajax_url, headers=ajax_headers, timeout=15)
        data_dict = ajax_res.json()
        rows = data_dict.get("aaData", [])
        for row_val in rows:
            if not isinstance(row_val, list):
                continue
                
            if len(row_val) < max(n_idx, m_idx) + 1:
                continue
                
            num_val = row_val[n_idx] if (0 <= n_idx < len(row_val)) else row_val[2]
            msg_val = row_val[m_idx] if (0 <= m_idx < len(row_val)) else row_val[4]
            
            clean_num = re.sub(r'\D', '', str(num_val))
            if clean_num and 5 <= len(clean_num) <= 18:
                otp = extract_otp_code(msg_val)
                if otp and len(msg_val) > 4:
                    results.append({"number": clean_num, "message": msg_val, "otp": otp})
                    
    else:
        # ৫.২ ডাইরেক্ট HTML টেবিল থেকে রিড করার ব্যাকআপ লজিক
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            if not rows: continue
            
            final_n_idx = n_idx
            final_m_idx = m_idx
            
            header_cells = rows[0].find_all(['th', 'td'])
            for i, cell in enumerate(header_cells):
                c_text = cell.get_text(strip=True).lower()
                if n_col_name in c_text: final_n_idx = i
                if m_col_name in c_text: final_m_idx = i

            for row in rows:
                cols = row.find_all(['td', 'th'])
                if all(c.name == 'th' for c in cols): continue
                
                if len(cols) > max(final_n_idx, final_m_idx):
                    num_text = cols[final_n_idx].get_text(separator=" ", strip=True)
                    msg_text = cols[final_m_idx].get_text(separator=" ", strip=True)
                    
                    clean_num = re.sub(r'\D', '', num_text)
                    if clean_num and 5 <= len(clean_num) <= 18:
                        otp = extract_otp_code(msg_text)
                        if otp and len(msg_text) > 4:
                            results.append({"number": clean_num, "message": msg_text, "otp": otp})
                            
    return results, html_text

# Track active number sessions to expire them automatically
user_active_sessions = {}
_otp_claim_lock = threading.Lock()

assigned_number_rates = {}

def claim_processed_otp(number, otp, legacy_ids=()):
    """Atomically suppress the same number+OTP, even when two listeners see it."""
    clean_number = re.sub(r"\D", "", str(number))
    clean_otp = str(otp).strip()
    if not clean_number or not clean_otp:
        return False
    unique_id = f"OTP_{clean_number}_{clean_otp}"
    with _otp_claim_lock:
        # Accept the identifiers written by older versions too.  Without this
        # migration check, the first restart after the fix could replay an OTP
        # that was already stored as PANEL_/VOLTX_/YESMS_/STEX_/POLL_ data.
        old_suffix = f"_{clean_number}_{clean_otp}"
        if (
            unique_id in processed_otps
            or any(str(item) == f"{clean_number}_{clean_otp}" or str(item).endswith(old_suffix)
                   for item in processed_otps)
            or any(item in processed_otps for item in legacy_ids)
        ):
            return False
        processed_otps.add(unique_id)
        # Do not clear the whole set: that caused old provider messages to be
        # delivered again whenever the set crossed the previous limit.
        if len(processed_otps) > 10000:
            processed_otps.pop()
        return True

def resolve_manual_service_rate(provider_key, service, rng):
    # 🌟 Manually added Voltx/StexSMS/Zebrasms service+country এর জন্য কাস্টম OTP রেট বের করা
    # provider_key: "stex" | "voltx" | "zebrasms"
    if not service:
        return bot_settings.get("otp_reward", 0.0)
    services_dict = bot_settings.get(f"{provider_key}_services", {}).get(service, {})
    for cnt, ranges in services_dict.items():
        if rng in ranges:
            rate = bot_settings.get(f"{provider_key}_service_rates", {}).get(service, {}).get(cnt)
            if rate is not None:
                return float(rate)
            break
    return bot_settings.get("otp_reward", 0.0)

# All persistent state lives in the portable local database file.
DATA_KEYS = [
    "number_batches", "used_numbers_list", "total_uploaded_stats", "total_assigned_stats",
    "stex_assigned_numbers", "voltx_assigned_numbers", "zebrasms_assigned_numbers",
    "yesms_assigned_numbers", "processed_otps", "assigned_number_rates",
]

def load_db():
    global bot_settings, number_batches, used_numbers_list, total_uploaded_stats, total_assigned_stats, stex_assigned_numbers, voltx_assigned_numbers, zebrasms_assigned_numbers, yesms_assigned_numbers, processed_otps, assigned_number_rates

    try:
        doc = None
        for attempt in range(3):
            try:
                doc = db.collection('settings').document('bot_config').get()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if doc.exists:
            fs_data = doc.to_dict()
            for k, val in fs_data.items():
                if k == "custom_messages":
                    for m_key, m_val in val.items():
                        bot_settings["custom_messages"][m_key] = m_val
                else:
                    bot_settings[k] = val
            for m_key, m_val in DEFAULT_CUSTOM_MESSAGES.items():
                if m_key not in bot_settings["custom_messages"]:
                    bot_settings["custom_messages"][m_key] = m_val
        else:
            db.collection('settings').document('bot_config').set({k: bot_settings[k] for k in FS_KEYS if k in bot_settings})
        print("✅ Config loaded from local file!")
    except Exception as e:
        print(f"❌ Error loading config from local file: {e}")

    try:
        doc = None
        for attempt in range(3):
            try:
                doc = db.collection('settings').document('bot_data').get()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        data = doc.to_dict() if doc.exists else {}
        number_batches = data.get("number_batches", {})
        used_numbers_list = data.get("used_numbers_list", [])
        total_uploaded_stats = data.get("total_uploaded_stats", 0)
        total_assigned_stats = data.get("total_assigned_stats", 0)
        stex_assigned_numbers = data.get("stex_assigned_numbers", {})
        voltx_assigned_numbers = data.get("voltx_assigned_numbers", {})
        zebrasms_assigned_numbers = data.get("zebrasms_assigned_numbers", {})
        yesms_assigned_numbers = data.get("yesms_assigned_numbers", {})
        processed_otps = set(data.get("processed_otps", []))
        assigned_number_rates = data.get("assigned_number_rates", {})
        print("✅ Data loaded from local file!")
    except Exception as e:
        print(f"❌ Error loading data from local file: {e}")

def _sync_fs():
    try:
        db.collection('settings').document('bot_config').set({k: bot_settings[k] for k in FS_KEYS if k in bot_settings})
        data = {
            "number_batches": number_batches, "used_numbers_list": used_numbers_list,
            "total_uploaded_stats": total_uploaded_stats, "total_assigned_stats": total_assigned_stats,
            "stex_assigned_numbers": stex_assigned_numbers, "voltx_assigned_numbers": voltx_assigned_numbers,
            "zebrasms_assigned_numbers": zebrasms_assigned_numbers, "yesms_assigned_numbers": yesms_assigned_numbers,
            "processed_otps": list(processed_otps), "assigned_number_rates": assigned_number_rates,
        }
        db.collection('settings').document('bot_data').set(data)
    except Exception as e:
        print(f"❌ Error saving to local file: {e}")

def save_db():
    # Saved in a background thread so it never slows down the bot's replies.
    threading.Thread(target=_sync_fs).start()

load_db()

def retry_local_load():
    # Keep the in-memory state aligned if the data file is replaced manually.
    while True:
        time.sleep(60)
        try:
            load_db()
        except Exception as e:
            print(f"Deferred local database reload failed: {e}")

user_states = {}
temp_data = {}
user_cooldowns = {}
pending_withdrawals = {}

# ==========================================
# Telegram API & Helpers
# ==========================================
tg_session = requests.Session() # 🌟 Keep-Alive Connection (Makes bot 10x faster)

def api_call(method, payload=None):
    url = f"{BASE_URL}/{method}"
    # getUpdates uses Telegram long-polling (timeout=50), so the HTTP
    # read timeout must be longer than the Telegram polling timeout.
    request_timeout = (10, 65) if method.startswith("getUpdates") else (10, 20)
    for attempt in range(3):
        try:
            res = tg_session.post(url, json=payload, timeout=request_timeout)
            res.raise_for_status()
            result = res.json()
            if result.get("ok"):
                return result
            description = result.get("description", "unknown Telegram API error")
            parameters = result.get("parameters", {})
            retry_after = parameters.get("retry_after")
            if retry_after and attempt < 2:
                time.sleep(min(int(retry_after), 10))
                continue
            print(f"Telegram API rejected {method}: {description}")
            return result
        except (requests.RequestException, ValueError) as e:
            if attempt == 2:
                print(f"Telegram API error ({method}): {e}")
                return {}
            time.sleep(2 ** attempt)

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("sendMessage", payload)

def send_photo(chat_id, photo_url_or_file_id, caption="", reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "photo": photo_url_or_file_id, "caption": caption, "parse_mode": parse_mode}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("sendPhoto", payload)

def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup: payload["reply_markup"] = reply_markup
    return api_call("editMessageText", payload)

def delete_message(chat_id, message_id):
    return api_call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

def answer_callback(callback_id, text="", show_alert=False):
    api_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": show_alert})

def generate_emoji_txt(mode):
    """Builds a .txt export in the exact format the flag/app TXT importer expects
    (see wait_for_flag_txt / wait_for_app_txt handling above)."""
    lines = []
    if mode == "flags":
        for code, data in bot_settings.get("premium_flags", {}).items():
            name = data.get("name", "")
            iso = data.get("iso", "")
            char = data.get("char", "")
            eid = data.get("id", "")
            payload = json.dumps({"emoji": char, "id": eid}, ensure_ascii=False)
            lines.append(f"{name} ({code})({iso}) {payload}")
    else:
        for name_key, data in bot_settings.get("premium_apps", {}).items():
            name = data.get("name", name_key)
            char = data.get("char", "")
            eid = data.get("id", "")
            payload = json.dumps({"emoji": char, "id": eid}, ensure_ascii=False)
            lines.append(f"{name} {payload}")
    if not lines:
        return None
    return "\n".join(lines)

def send_document(chat_id, filename, text_content):
    url = f"{BASE_URL}/sendDocument"
    files = {'document': (filename, text_content)}
    data = {'chat_id': chat_id}
    try:
        requests.post(url, data=data, files=files, timeout=(10, 30))
    except requests.RequestException as e:
        print(f"Telegram document upload error: {e}")

# In-memory cache of known user IDs, loaded once from the local database.
all_known_users = set()

def sync_users_list():
    global all_known_users
    try:
        for doc in db.collection('users').select([]).stream():
            all_known_users.add(doc.id)
    except: pass

threading.Thread(target=sync_users_list, daemon=True).start()

def register_user_local(uid):
    all_known_users.add(str(uid))

def broadcast_copymessage(from_chat_id, msg_id):
    success = 0
    failed = 0
    users = list(all_known_users)
    
    # 🌟 Dedicated Connection Pool for Broadcast (Fixes Port Exhaustion & Network Lag)
    b_session = requests.Session()
    url = f"{BASE_URL}/copyMessage"
    
    for user_id in users:
        payload = {"chat_id": user_id, "from_chat_id": from_chat_id, "message_id": msg_id}
        try:
            res = b_session.post(url, json=payload, timeout=5).json()
            if res.get("ok"): success += 1
            else: failed += 1
        except:
            failed += 1
        time.sleep(0.035) # Safe speed (28 msgs/sec) to prevent Telegram Ban
        
    send_message(from_chat_id, render_body_text(f"📢 <b>Broadcast Completed!</b>\n✅ Success: {success}\n❌ Failed: {failed}\n👥 Total Sent: {len(users)}"))

def render_body_text(text):
    if not text: return str(text)
    parts = re.split(r'(<tg-emoji.*?</tg-emoji>)', str(text))
    for i in range(len(parts)):
        if not parts[i].startswith('<tg-emoji'):
            for normal_emj, prem_id in GLOBAL_BODY_EMOJIS.items():
                if normal_emj in parts[i]:
                    parts[i] = parts[i].replace(normal_emj, f'<tg-emoji emoji-id="{prem_id}">{normal_emj}</tg-emoji>')
    return "".join(parts)

def extract_premium_html(msg):
    text = msg.get("text", msg.get("caption", ""))
    entities = msg.get("entities", msg.get("caption_entities", []))
    if not entities: return text
    try:
        b_text = text.encode('utf-16-le')
        c_entities = [e for e in entities if e.get("type") == "custom_emoji"]
        c_entities.sort(key=lambda x: x["offset"], reverse=True)
        for ent in c_entities:
            offset = ent["offset"] * 2
            length = ent["length"] * 2
            eid = ent["custom_emoji_id"]
            emoji_char = b_text[offset:offset+length].decode('utf-16-le')
            html_tag = f'<tg-emoji emoji-id="{eid}">{emoji_char}</tg-emoji>'
            replacement = html_tag.encode('utf-16-le')
            b_text = b_text[:offset] + replacement + b_text[offset+length:]
        return b_text.decode('utf-16-le')
    except Exception as e:
        return text 

def country_code_from_label(country):
    """Return the ISO code from BD, Bangladesh, or Bangladesh (BD)."""
    value = str(country or "").strip().upper()
    match = re.search(r"\(([A-Z]{2})\)\s*$", value)
    if match:
        return match.group(1)
    if value in COUNTRY_META:
        return value
    for iso, (name, _) in COUNTRY_META.items():
        if value == name.upper():
            return iso
    for flag_data in bot_settings.get("premium_flags", {}).values():
        if value == str(flag_data.get("name", "")).strip().upper():
            return str(flag_data.get("iso", "")).upper()
    return ""

def country_display_name(country):
    """Store/display countries consistently as Full Name (ISO).

    Also supports a trailing number to create a 2nd/3rd entry for the same
    country, e.g. "MG 2" -> "Madagascar 2 (MG)", "MG 3" -> "Madagascar 3 (MG)".
    This is used identically by every panel (StexSMS/Voltx/Zebrasms/Yesms).
    """
    raw = str(country or "").strip()
    suffix = ""
    m = re.match(r"^(.*\S)\s+(\d+)$", raw)
    base = raw
    if m and country_code_from_label(m.group(1)):
        base = m.group(1)
        suffix = f" {m.group(2)}"
    iso = country_code_from_label(base)
    # Use the admin's premium flag database for every ISO code, not only the
    # small built-in metadata list.
    for flag_data in bot_settings.get("premium_flags", {}).values():
        if flag_data.get("iso", "").upper() == iso:
            return f"{flag_data.get('name', base)}{suffix} ({iso})"
    if not iso:
        return raw
    return f"{COUNTRY_META.get(iso, (base.title(), ''))[0]}{suffix} ({iso})"

def country_calling_code(country):
    iso = country_code_from_label(country)
    for calling_code, flag_data in bot_settings.get("premium_flags", {}).items():
        if flag_data.get("iso", "").upper() == iso and str(calling_code).isdigit():
            return str(calling_code)
    return COUNTRY_META.get(iso, ("", ""))[1]

def normalize_provider_number(number, country=""):
    """Convert a national Yesms number to an international number."""
    clean = re.sub(r"[^\d]", "", str(number or ""))
    if not clean:
        return ""
    calling = country_calling_code(country)
    if calling and not clean.startswith(calling):
        clean = clean.lstrip("0")
        clean = calling + clean
    return clean

def provider_country(provider, service, range_id):
    """Find the country configured for a provider range."""
    services_key = f"{provider}_services"
    for country, ranges in bot_settings.get(services_key, {}).get(service or "", {}).items():
        if str(range_id) in [str(r) for r in ranges]:
            return country
    return ""

def unicode_flag(iso):
    iso = str(iso or "").upper()
    if len(iso) != 2 or not iso.isalpha():
        return "🌍"
    return "".join(chr(127397 + ord(c)) for c in iso)

def get_country_flag_info(country):
    """Resolve a country flag by exact ISO/name matching only."""
    raw = str(country or "").strip()
    iso = country_code_from_label(raw)
    for flag_data in bot_settings.get("premium_flags", {}).values():
        flag_iso = flag_data.get("iso", "").upper()
        flag_name = str(flag_data.get("name", "")).strip().upper()
        if (iso and flag_iso == iso) or (not iso and raw.upper() == flag_name):
            return flag_data.get("char", unicode_flag(flag_iso)), flag_data.get("id")
    return (unicode_flag(iso) if iso else "🌍"), None

def get_flag_info_from_num(num):
    clean = re.sub(r"[^\d]", "", str(num or ""))
    sorted_codes = sorted(bot_settings.get("premium_flags", {}).keys(), key=len, reverse=True)
    for code in sorted_codes:
        if clean.startswith(code):
            data = bot_settings["premium_flags"][code]
            return data["char"], data.get("iso", "XX"), data.get("id")
    # Fallback for countries that do not have a custom Telegram emoji entry.
    for iso, (_, calling) in COUNTRY_META.items():
        if calling and clean.startswith(calling):
            return unicode_flag(iso), iso, None
    return "🌍", "XX", None

def get_flag_and_code(num):
    char, iso, _ = get_flag_info_from_num(num)
    return char, iso

def get_flag_info_html(num_or_iso, return_full_name=False):
    iso_hint = country_code_from_label(num_or_iso)
    if iso_hint:
        num_or_iso = iso_hint
    if len(str(num_or_iso)) == 2:
        for code, data in bot_settings.get("premium_flags", {}).items():
            if data.get("iso", "").upper() == str(num_or_iso).upper():
                eid = data.get("id")
                char = data.get("char")
                name = data.get("name", num_or_iso)
                if return_full_name: return name
                if eid: return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
                return char
        if str(num_or_iso).upper() in COUNTRY_META:
            iso = str(num_or_iso).upper()
            if return_full_name:
                return COUNTRY_META[iso][0]
            return unicode_flag(iso)
        if return_full_name: return num_or_iso
        return "🌍"
        
    char, detected_iso, eid = get_flag_info_from_num(num_or_iso)
    if return_full_name:
        for code, data in bot_settings.get("premium_flags", {}).items():
            clean = re.sub(r"[^\d]", "", str(num_or_iso))
            if clean.startswith(code): return data.get("name", num_or_iso)
        if detected_iso in COUNTRY_META:
            return COUNTRY_META[detected_iso][0]
        return num_or_iso
        
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
    return char

def mask_number(num):
    clean = num.replace("+", "").replace(" ", "")
    if len(clean) > 6: return f"{clean[:3]}TGZ{clean[-3:]}"
    elif len(clean) > 2: return f"{clean[:1]}TGZ{clean[-1:]}"
    return clean

LANG_MAP = {
    "#EN": "English", "#BN": "Bengali", "#AR": "Arabic", "#HI": "Hindi", 
    "#PA": "Punjabi", "#GU": "Gujarati", "#OR": "Odia", "#TA": "Tamil", 
    "#TE": "Telugu", "#KN": "Kannada", "#ML": "Malayalam", "#SI": "Sinhala", 
    "#TH": "Thai", "#LO": "Lao", "#BO": "Tibetan", "#MY": "Burmese", 
    "#AM": "Amharic", "#KM": "Khmer", "#KA": "Georgian", "#HY": "Armenian", 
    "#HE": "Hebrew", "#EL": "Greek", "#RU": "Russian", "#ZH": "Chinese", 
    "#JA": "Japanese", "#KO": "Korean", "#ID": "Indonesian", "#MS": "Malay", 
    "#VN": "Vietnamese", "#TL": "Filipino", "#ES": "Spanish", "#PT": "Portuguese", 
    "#FR": "French", "#DE": "German", "#IT": "Italian", "#PL": "Polish", 
    "#TR": "Turkish", "#NL": "Dutch", "#SV": "Swedish", "#DA": "Danish", 
    "#NO": "Norwegian", "#FI": "Finnish", "#CS": "Czech", "#SK": "Slovak", 
    "#HU": "Hungarian", "#RO": "Romanian", "#HR": "Croatian", "#BG": "Bulgarian", 
    "#UK": "Ukrainian", "#SW": "Swahili", "#AF": "Afrikaans"
}

# ==========================================
# 🌟 ADVANCED SERVICE & LANGUAGE DETECTION
# ==========================================

SERVICE_SMS_KEYWORDS = {
    # 🟢 Social Media & Chat (Added Arabic Keywords)
    "whatsapp": ["whatsapp", "whatsa", "whatsap", "whats", "whatsapp business", "whatsapp me", "whatsapp code", "whatsap", "واتساب", "واتساپ", "واٹس ایپ", "व्हाट्सएप", "वाट्सएप", "वॉट्सऐप", "व्हाट्सप्प", "হোয়াটসঅ্যাপ", "হোটসঅ্যাপ", "ватсап", "уотсап", "вотсап", "ватс апп", "వాట్సాప్", "വാട്‌സ്ആപ്പ്", "வாட்ஸ்அப்", "ವಾಟ್ಸಾಪ್", "વોટ્સએપ", "ਵਟਸਐਪ", "ହ୍ଵାଟସ୍ ଆପ୍", "වට්ස්ඇප්", "วอตส์แอปป์", "วอทส์แอพ", "ဝက်စ်အက်ပ်", "វ៉តសាប់", "ວອດແອັບ", "ワッツアップ", "왓츠앱", "whatsapp的", "whatsapp验证码", "וואטסאפ", "γουάτσαπ", "ዋትስአፕ", "ვოთსאფი", "վոթսափ"],
    "facebook": ["facebook", "fb", "meta", "fbook", "fb code", "facebook code", "فيسبوك", "فيس بوك"],
    "instagram": ["instagram", "insta", "ig", "ig code", "instagram code", "انستغرام", "انستقرام"],
    "telegram": ["telegram", "tg", "tele", "telegram code", "tg code", "t.me", "تيليجرام", "تليجرام"],
    "tiktok": ["tiktok", "tik tok", "tikvideo", "tiktok code", "tik code", "تيك توك"],
    "snapchat": ["snapchat", "snap", "snap code", "سناب شات"],
    "twitter": ["twitter", "x.com", "x code", "twitter code", "تويتر"],
    "discord": ["discord", "discord code", "ديسكورد"],
    "viber": ["viber", "viber code", "فايبر"],
    "line": ["line", "line code", "line verification", "لاين"],
    "wechat": ["wechat", "we chat", "wechat code", "وي تشات"],
    "signal": ["signal", "signal code", "سيجنال"],
    "linkedin": ["linkedin", "linked in", "لينكد إن"],
    "imo": ["imo", "imo code", "imo verification", "ايمو"],
    "kakaotalk": ["kakao", "kakaotalk", "كاكاو"],
    "qq": ["qq", "tencent qq"],
    "vk": ["vk", "vkontakte"],

    # 🔵 Tech & Mail
    "google": ["google", "gmail", "youtube", "g-", "google voice", "جوجل", "غوغل"],
    "microsoft": ["microsoft", "ms", "outlook", "live.com", "hotmail"],
    "apple": ["apple", "icloud", "itunes", "apple id"],
    "yahoo": ["yahoo", "yahoo code", "ymail"],
    "protonmail": ["proton", "protonmail"],
    
    # 💰 Crypto & Trading
    "binance": ["binance", "bnb", "binances"],
    "coinbase": ["coinbase"],
    "okx": ["okx", "okex"],
    "kucoin": ["kucoin"],
    "bybit": ["bybit"],
    "huobi": ["huobi", "htx"],
    "mexc": ["mexc"],
    "trustwallet": ["trust wallet", "trustwallet"],

    # 💳 Finance & Wallets
    "bkash": ["bkash", "b-kash", "bkash code"],
    "nagad": ["nagad", "nagad code"],
    "rocket": ["rocket", "dutch bangla"],
    "upay": ["upay", "upay code"],
    "paypal": ["paypal", "pay pal"],
    "paytm": ["paytm"],
    "cashapp": ["cash app", "cashapp"],
    "wise": ["wise", "transferwise"],

    # 🛒 E-commerce & Delivery
    "amazon": ["amazon", "amzn", "amazon code"],
    "ebay": ["ebay"],
    "aliexpress": ["aliexpress", "ali express"],
    "alibaba": ["alibaba"],
    "daraz": ["daraz", "daraz code"],
    "foodpanda": ["foodpanda", "food panda"],
    "uber": ["uber", "uber code", "uber verification", "uber eats"],
    "pathao": ["pathao", "pathao ride"],

    # 🎮 Gaming & Entertainment
    "netflix": ["netflix", "netflix code"],
    "spotify": ["spotify", "spotify code"],
    "steam": ["steam", "steam guard"],
    "epicgames": ["epic games", "epicgames"],
    "roblox": ["roblox", "roblox code"],
    "riotgames": ["riot", "riot games", "valorant", "league of legends"],
    "garena": ["garena", "free fire", "freefire"],
    "playstation": ["playstation", "psn"],

    # 🎲 Betting & Casino
    "1xbet": ["1xbet", "1x bet"],
    "melbet": ["melbet", "melbet code"],
    "linebet": ["linebet"],
    "bet365": ["bet365"],
    "megapari": ["megapari"],

    # ❤️ Dating
    "tinder": ["tinder", "tinder code"],
    "bumble": ["bumble"],
    "badoo": ["badoo"]
}

def detect_service(text):
    text_lower = str(text).lower()
    for service_key, keywords in SERVICE_SMS_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                return service_key.upper()
    return None

def get_service_info_html(service_text, msg_text=""):
    s = str(service_text).upper().strip()
    m = str(msg_text).lower().strip()
    apps = bot_settings.get("premium_apps", {})
    
    detected_service = s
    if m:
        for service_key, keywords in SERVICE_SMS_KEYWORDS.items():
            for kw in keywords:
                if kw in m:
                    detected_service = service_key.upper()
                    break
            if detected_service != s: break

    clean_s = re.sub(r'[^\w\s]', '', detected_service).strip()
    
    for app_name, data in apps.items():
        if app_name == detected_service or app_name == clean_s or app_name in detected_service or detected_service in app_name:
            full_name = data.get("name", app_name.title())
            char = data.get("char", "📱")
            eid = data.get("id")
            if eid: return full_name, f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>'
            return full_name, char
            
    if len(detected_service) > 20:
        return "Message", "💬"
        
    return detected_service.title(), "📱"

def detect_language(text):
    if not text: return "#EN"
    text_str = str(text)

    # ১. Unicode Block দিয়ে নিখুঁত বর্ণমালা শনাক্তকরণ (100% Accurate for scripts)
    if any('\u0600' <= c <= '\u06ff' for c in text_str): return "#AR" # Arabic / Persian / Urdu
    if any('\u0980' <= c <= '\u09ff' for c in text_str): return "#BN" # Bengali
    if any('\u0900' <= c <= '\u097f' for c in text_str): return "#HI" # Hindi / Marathi / Nepali
    if any('\u0a00' <= c <= '\u0a7f' for c in text_str): return "#PA" # Punjabi (Gurmukhi)
    if any('\u0a80' <= c <= '\u0aff' for c in text_str): return "#GU" # Gujarati
    if any('\u0b00' <= c <= '\u0b7f' for c in text_str): return "#OR" # Odia
    if any('\u0b80' <= c <= '\u0bff' for c in text_str): return "#TA" # Tamil
    if any('\u0c00' <= c <= '\u0c7f' for c in text_str): return "#TE" # Telugu
    if any('\u0c80' <= c <= '\u0cff' for c in text_str): return "#KN" # Kannada
    if any('\u0d00' <= c <= '\u0d7f' for c in text_str): return "#ML" # Malayalam
    if any('\u0d80' <= c <= '\u0dff' for c in text_str): return "#SI" # Sinhala
    if any('\u0e00' <= c <= '\u0e7f' for c in text_str): return "#TH" # Thai
    if any('\u0e80' <= c <= '\u0eff' for c in text_str): return "#LO" # Lao
    if any('\u0f00' <= c <= '\u0fff' for c in text_str): return "#BO" # Tibetan
    if any('\u1000' <= c <= '\u109f' for c in text_str): return "#MY" # Burmese (Myanmar)
    if any('\u1200' <= c <= '\u137f' for c in text_str): return "#AM" # Amharic (Ethiopic)
    if any('\u1780' <= c <= '\u17ff' for c in text_str): return "#KM" # Khmer
    if any('\u10a0' <= c <= '\u10ff' for c in text_str): return "#KA" # Georgian
    if any('\u0530' <= c <= '\u058f' for c in text_str): return "#HY" # Armenian
    if any('\u0590' <= c <= '\u05ff' for c in text_str): return "#HE" # Hebrew
    if any('\u0370' <= c <= '\u03ff' for c in text_str): return "#EL" # Greek
    if any('\u0400' <= c <= '\u04ff' for c in text_str): return "#RU" # Russian / Ukrainian (Cyrillic)
    if any('\u4e00' <= c <= '\u9fff' for c in text_str): return "#ZH" # Chinese
    if any('\u3040' <= c <= '\u309f' or '\u30a0' <= c <= '\u30ff' for c in text_str): return "#JA" # Japanese
    if any('\uac00' <= c <= '\ud7af' for c in text_str): return "#KO" # Korean

    # ২. OTP Keyword দিয়ে ভাষা শনাক্তকরণ (Latin script languages)
    text_lower = text_str.lower()
    
    # Asian / Pacific
    if any(w in text_lower for w in ["kode verifikasi", "jangan bagikan", "rahasia"]): return "#ID" # Indonesian
    if any(w in text_lower for w in ["kod pengesahan", "jangan kongsi"]): return "#MS" # Malay
    if any(w in text_lower for w in ["mã của bạn", "không chia sẻ", "mã xác minh"]): return "#VN" # Vietnamese
    if any(w in text_lower for w in ["ang iyong code", "huwag ibahagi"]): return "#TL" # Tagalog / Filipino
    
    # European / Americas
    if any(w in text_lower for w in ["código", "tu código", "verificación", "no compartas"]): return "#ES" # Spanish
    if any(w in text_lower for w in ["seu código", "código de verificação", "não compartilhe"]): return "#PT" # Portuguese
    if any(w in text_lower for w in ["code secret", "ne partagez pas", "votre code"]): return "#FR" # French
    if any(w in text_lower for w in ["dein code", "bestätigungscode", "nicht teilen"]): return "#DE" # German
    if any(w in text_lower for w in ["il tuo codice", "codice di verifica", "non condividere"]): return "#IT" # Italian
    if any(w in text_lower for w in ["twój kod", "nie udostępniaj", "kod weryfikacyjny"]): return "#PL" # Polish
    if any(w in text_lower for w in ["doğrulama kodu", "paylaşmayın", "onay kodu"]): return "#TR" # Turkish
    if any(w in text_lower for w in ["jouw code", "verificatiecode", "niet delen"]): return "#NL" # Dutch
    if any(w in text_lower for w in ["din kod", "verifieringskod", "dela inte"]): return "#SV" # Swedish
    if any(w in text_lower for w in ["bekræftelseskode", "del ikke"]): return "#DA" # Danish
    if any(w in text_lower for w in ["bekreftelseskode", "ikke del"]): return "#NO" # Norwegian
    if any(w in text_lower for w in ["vahvistuskoodi", "älä jaa"]): return "#FI" # Finnish
    if any(w in text_lower for w in ["váš kód", "ověřovací kód", "nesdílejte"]): return "#CS" # Czech
    if any(w in text_lower for w in ["overovací kód", "nezdieľajte"]): return "#SK" # Slovak
    if any(w in text_lower for w in ["ellenőrző kód", "ne oszd meg"]): return "#HU" # Hungarian
    if any(w in text_lower for w in ["codul tău", "codul de verificare", "nu partaja"]): return "#RO" # Romanian
    if any(w in text_lower for w in ["kontrolni kod", "kod za potvrdu", "ne delite"]): return "#HR" # Croatian/Serbian
    if any(w in text_lower for w in ["код за потвърждение", "не споделяйте"]): return "#BG" # Bulgarian
    if any(w in text_lower for w in ["ваш код", "код підтвердження"]): return "#UK" # Ukrainian
    
    # African
    if any(w in text_lower for w in ["msimbo wako", "usishiriki"]): return "#SW" # Swahili
    if any(w in text_lower for w in ["verifikasiekode", "moenie deel nie"]): return "#AF" # Afrikaans
    
    # ৩. উপরের কোনোটি না মিললে ডিফল্ট
    return "#EN"

def parse_chat_id(text):
    text = text.strip()
    if text.startswith("-100") or (text.startswith("-") and text[1:].isdigit()):
        return text
    if "t.me/" in text:
        parts = text.split("/")
        username = parts[-1]
        if username: return "@" + username if not username.startswith("@") else username
    if text.startswith("@"):
        return text
    return "@" + text

def is_admin(user_id):
    return user_id in bot_settings["admins"] or user_id == OWNER_ID

def check_force_join(user_id):
    if not bot_settings["fj_on"] or not bot_settings["fj_channels"]: return True
    if is_admin(user_id): return True
    for ch in bot_settings["fj_channels"]:
        res = api_call("getChatMember", {"chat_id": ch, "user_id": user_id})
        if res.get("ok") and res["result"]["status"] not in ["left", "kicked"]: continue
        else: return False
    return True

def send_force_join_msg(chat_id):
    kb = []
    for ch in bot_settings["fj_channels"]:
        url = f"https://t.me/{ch.replace('@', '')}" if ch.startswith("@") else ch
        kb.append([{"text": f"Join Channel", "icon_custom_emoji_id": "5789428375261023681", "url": url, "style": "primary"}])
    kb.append([{"text": "Check Joined", "icon_custom_emoji_id": "5352694861990501856", "callback_data": "check_fj", "style": "success"}])
    send_message(chat_id, render_body_text(f"{PEM['warn']} <b>Please join our channels to use the bot!</b>"), reply_markup={"inline_keyboard": kb})

def is_user_banned(user_id):
    if is_admin(user_id): return False
    if user_id in user_banned_cache and time.time() - user_banned_cache[user_id]['time'] < 60:
        return user_banned_cache[user_id]['banned']
    banned = False
    if db:
        try:
            doc = db.collection('users').document(str(user_id)).get()
            banned = doc.exists and doc.to_dict().get("banned", False)
        except: pass
    user_banned_cache[user_id] = {'banned': banned, 'time': time.time()}
    return banned

# ==========================================
# Captcha Auto Login & Parsing Core
# ==========================================
def extract_otp_code(text):
    clean_text = re.sub(r'[\u200B-\u200D\uFEFF]', '', str(text))

    # 1. Multi-part OTPs (e.g. 123-456 or 809-761)
    multi_part = re.search(r'(\d{3}[-\s]+\d{3})|(\d{2}[-\s]+\d{2}[-\s]+\d{2})', clean_text)
    if multi_part:
        # হাইফেন (-) থাকলে সেটা রেখে দিবে, কিন্তু স্পেস থাকলে মুছে একসাথে করে দিবে
        return multi_part.group(0).replace(" ", "")

    # 2. Keyword-based extraction
    otp_keywords = ['code', 'is', 'otp', 'pin', 'verification', 'auth', 'কোড', 'رمز', 'your code']
    keywords_pattern = '|'.join(otp_keywords)
    keyword_match = re.search(rf'(?:{keywords_pattern})\s*(?:is|:|-|=)?\s*([a-z0-9]{{4,10}})', clean_text, re.I)
    if keyword_match and keyword_match.group(1).isdigit():
        return keyword_match.group(1)
        
    keyword_match_rev = re.search(rf'([a-z0-9]{{4,10}})\s*(?:is your|is the|কোড)', clean_text, re.I)
    if keyword_match_rev and keyword_match_rev.group(1).isdigit():
        return keyword_match_rev.group(1)

    # 3. Google OTP
    g_match = re.search(r'G-(\d{6})', clean_text, re.IGNORECASE)
    if g_match: return g_match.group(1)

    # 4. Digit sequences fallback
    digit_matches = re.findall(r'(?<!\d)\d{4,8}(?!\d)', clean_text)
    if digit_matches: return digit_matches[0]

    return None

def parse_panel_response(response_text, p_config=None):
    results = []
    p_type = p_config.get("type", "API Panel") if p_config else "API Panel"
    
    n_col_name = p_config.get("num_col_name", "number").lower() if p_config else "number"
    m_col_name = p_config.get("msg_col_name", "message").lower() if p_config else "message"
    n_idx = int(p_config.get("num_col_idx", 1)) - 1 if p_config and p_config.get("num_col_idx") else 1
    m_idx = int(p_config.get("msg_col_idx", 2)) - 1 if p_config and p_config.get("msg_col_idx") else 2

    if p_type == "Auto Captcha Panel":
        try:
            soup = BeautifulSoup(response_text, 'html.parser')
            tables = soup.find_all('table')
            
            for table in tables:
                rows = table.find_all('tr')
                if not rows: continue
                
                # 🌟 Option 1 + Smart HTML Detection: কলামের নাম ও ব্যবহারকারীর দেওয়া সিরিয়াল দিয়ে সঠিক পজিশন বের করা
                final_n_idx = n_idx
                final_m_idx = m_idx
                
                # প্রথম রো (Header) চেক করে কলামের আসল সিরিয়াল মিলিয়ে নেওয়া
                header_cells = rows[0].find_all(['th', 'td'])
                for i, cell in enumerate(header_cells):
                    c_text = cell.get_text(strip=True).lower()
                    if n_col_name in c_text: final_n_idx = i
                    if m_col_name in c_text: final_m_idx = i

                for row in rows:
                    cols = row.find_all(['td', 'th'])
                    
                    # হেডার রো (যেখানে সব th থাকে) সেগুলো থেকে ডাটা নিবে না
                    if all(c.name == 'th' for c in cols): continue
                    
                    if len(cols) > max(final_n_idx, final_m_idx):
                        # HTML টেবিল থেকে টেক্সট বের করা
                        num_text = cols[final_n_idx].get_text(separator=" ", strip=True)
                        msg_text = cols[final_m_idx].get_text(separator=" ", strip=True)
                        
                        clean_num = re.sub(r'\D', '', num_text)
                        
                        # নাম্বারটা আসলেই ৫-১৮ ডিজিটের কিনা তা নিশ্চিত করা (যাতে উল্টাপাল্টা টেক্সট না আসে)
                        if clean_num and 5 <= len(clean_num) <= 18:
                            otp = extract_otp_code(msg_text)
                            if otp and len(msg_text) > 4:
                                results.append({"number": clean_num, "message": msg_text, "otp": otp})
        except Exception as e:
            pass
    else:
        try:
            data = json.loads(response_text)
            temp_results = []
            
            def process_item(item):
                pot_nums_list = []
                pot_msg = None
                values = []
                
                if isinstance(item, dict):
                    # ১. প্রথমে পরিচিত JSON Key (যেমন: num, phone, sms) দিয়ে খোঁজার চেষ্টা
                    lower_keys = {str(k).lower(): v for k, v in item.items()}
                    for k in ["number", "num", "phone", "msisdn", "sender"]:
                        if k in lower_keys:
                            clean_val = re.sub(r'\D', '', str(lower_keys[k]))
                            if 5 <= len(clean_val) <= 18:
                                if clean_val not in pot_nums_list: pot_nums_list.append(clean_val)
                    for k in ["message", "msg", "sms", "content", "text"]:
                        if k in lower_keys:
                            val = str(lower_keys[k])
                            if len(val) > 4:
                                pot_msg = val
                                break
                    values = list(item.values())
                elif isinstance(item, list):
                    values = item

                # ২. যদি Key দিয়ে না পাওয়া যায়, তবে Smart Blind Scan (সব ভ্যালু চেক করবে)
                for v in values:
                    if isinstance(v, (dict, list)) or v is None: continue
                    v_str = str(v).strip()
                    
                    # Number Detection: 7 থেকে 18 ডিজিট
                    clean_v = re.sub(r'\D', '', v_str)
                    if 7 <= len(clean_v) <= 18 and not re.search(r'[a-zA-Z]', v_str):
                        # Date/Time/IP এড়ানোর লজিক
                        if not re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', v_str) and not re.search(r'\d{2}:\d{2}:\d{2}', v_str) and "." not in v_str:
                            if clean_v not in pot_nums_list:
                                pot_nums_list.append(clean_v)
                    
                    # Message Detection: 5 অক্ষরের বেশি এবং শুধু সংখ্যা নয়
                    if len(v_str) > 4 and not v_str.isdigit():
                        if extract_otp_code(v_str):
                            if pot_msg is None or len(v_str) > len(pot_msg):
                                pot_msg = v_str
                                
                # 🌟 ৩. Multiple Numbers Logic (User Priority > Second Number > First Number)
                pot_num = None
                if pot_nums_list:
                    matched_user_num = None
                    for n in pot_nums_list:
                        # চেক করবে ইউজারের অ্যাসাইন করা নাম্বারের তালিকায় এই নাম্বারটি আছে কি না
                        if n in stex_assigned_numbers or any(n in str(key) for key in stex_assigned_numbers.keys()):
                            matched_user_num = n
                            break
                    
                    if matched_user_num:
                        pot_num = matched_user_num
                    elif len(pot_nums_list) >= 2:
                        pot_num = pot_nums_list[1] # ইউজারের কাছে না থাকলে সরাসরি দ্বিতীয় নাম্বারটি নেবে
                    else:
                        pot_num = pot_nums_list[0]
                            
                if pot_num and pot_msg:
                    otp = extract_otp_code(pot_msg)
                    if otp:
                        temp_results.append({"number": pot_num, "message": pot_msg, "otp": otp})
                        
            def traverse_json(node):
                if isinstance(node, list):
                    if len(node) > 0 and not isinstance(node[0], (dict, list)):
                        # It's a flat list representing one record
                        process_item(node)
                    for child in node:
                        if isinstance(child, (dict, list)):
                            traverse_json(child)
                elif isinstance(node, dict):
                    process_item(node)
                    for val in node.values():
                        if isinstance(val, (dict, list)):
                            traverse_json(val)

            traverse_json(data)
            
            # Remove duplicates
            seen = set()
            for r in temp_results:
                uid = f"{r['number']}_{r['otp']}"
                if uid not in seen:
                    seen.add(uid)
                    results.append(r)
        except: pass
        
    return results

# 🌟 Advanced Automated Background Captcha Solver 🌟
def attempt_auto_login(p, idx):
    login_url = p.get("login_url", "").strip()
    if not login_url.startswith("http"):
        login_url = "http://" + login_url
        
    if not login_url.lower().endswith('/login') and not login_url.lower().endswith('.php'):
        login_url = f"{login_url.rstrip('/')}/login"
        
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    })
    
    try:
        res = session.get(login_url, timeout=15)
        soup = BeautifulSoup(res.text, 'html.parser')
        all_text = res.text
        
        # 1. SOLVE CAPTCHA (Exact bot 3.py logic)
        captcha_match = re.search(r'(\d+\s*[\+\-\*]\s*\d+)\s*[=\?:]', all_text)
        if not captcha_match:
            captcha_match = re.search(r'what is\s*(\d+\s*[\+\-\*]\s*\d+)', all_text, re.I)
        if not captcha_match:
            elements = soup.find_all(["label", "div", "span", "p", "strong"])
            for el in elements:
                txt = el.get_text(separator=" ", strip=True)
                if any(op in txt for op in ["+", "-", "*"]):
                    m = re.search(r'(\d+\s*[\+\-\*]\s*\d+)', txt)
                    if m:
                        captcha_match = m
                        break
                        
        captcha_text = captcha_match.group(1) if captcha_match else "0 + 0"
        answer = "0"
        m2 = re.search(r'(\d+)\s*([\+\-\*])\s*(\d+)', captcha_text)
        if m2:
            a, op, b = int(m2.group(1)), m2.group(2), int(m2.group(3))
            if op == '+': answer = str(a + b)
            elif op == '-': answer = str(a - b)
            elif op == '*': answer = str(a * b)

        # 2. FIND FORM
        form = soup.find("form")
        if not form:
            p["login_status"] = "❌ No login form found"
            return False
            
        action = form.get("action")
        from urllib.parse import urljoin
        post_url = urljoin(login_url, action) if action else login_url

        form_data = {}
        for hidden in form.find_all("input", type="hidden"):
            name = hidden.get("name")
            if name: form_data[name] = hidden.get("value") or ""
        
        user_input = form.find("input", {"name": re.compile(r"user|email|id", re.I)}) or \
                     form.find("input", {"type": "text", "placeholder": re.compile(r"user|email", re.I)}) or \
                     form.find("input", {"type": "text"})
                     
        pass_input = form.find("input", {"name": re.compile(r"pass", re.I)}) or \
                     form.find("input", {"type": "password"})
                     
        captcha_input = form.find("input", {"placeholder": re.compile(r"answer|ans|code|verification|value|captcha", re.I)}) or \
                        form.find("input", {"name": re.compile(r"ans|captcha|ver|code", re.I)})
        
        user_field = user_input.get("name") if user_input else "username"
        pass_field = pass_input.get("name") if pass_input else "password"
        captcha_field = captcha_input.get("name") if captcha_input else "answer"

        form_data[user_field] = p.get("username", "")
        form_data[pass_field] = p.get("password", "")
        if captcha_field:
            form_data[captcha_field] = answer

        # 3. SUBMIT
        login_req = session.post(post_url, data=form_data, allow_redirects=True, timeout=15)
        
        # 4. VERIFY (Exact bot 3.py check logic)
        msg_link = p.get("msg_link", "").strip()
        if not msg_link.startswith("http") and msg_link != "":
            msg_link = "http://" + msg_link
            
        check_url = msg_link if msg_link else f"{login_url.split('/login')[0]}/client/SMSCDRStats"
        
        check_res = session.get(check_url, timeout=10)
        
        if 'logout' in login_req.text.lower() or 'logout' in check_res.text.lower() or 'sms reports' in check_res.text.lower() or 'dashboard' in check_res.text.lower() or 'cdrs' in check_res.text.lower():
            panel_sessions[idx] = session
            p["login_status"] = "✅ Active & Fetching"
            return True
        else:
            # এখানে ফেইল হলে অংক কী পেয়েছিল তা দেখা যাবে
            p["login_status"] = f"❌ Login Failed (Math: {captcha_text} = {answer})"
            return False
            
    except Exception as e:
        p["login_status"] = f"❌ Error: {str(e)[:20]}"
        
    return False

def panel_monitor_thread():
    global processed_otps, panel_sessions
    while True:
        try:
            for idx, p in enumerate(bot_settings.get("panels", [])):
                if p.get("status") == "ON":
                    
                    if p.get("type") == "Auto Captcha Panel":
                        sess = panel_sessions.get(idx)
                        
                        if not sess:
                            now = time.time()
                            if now - p.get("last_login_attempt", 0) < 30: 
                                continue 
                            p["last_login_attempt"] = now
                            
                            success = attempt_auto_login(p, idx)
                            save_db() # Save login status text to show in settings
                            if not success:
                                continue 
                            sess = panel_sessions.get(idx)
                            
                        try:
                            # 🌟 auto sessions with sAjaxSource and Fallback HTML Parser
                            parsed_data, res_text = fetch_cpt_panel_cdrs(p, sess, p["msg_link"])
                            p["login_status"] = "✅ Active & Fetching"
                        except Exception as e:
                            p["login_status"] = "❌ Session Expired (Retrying...)"
                            del panel_sessions[idx]
                            save_db()
                            continue

                    elif p.get("api_url") or p.get("full_api_url"): 
                        full_url = p.get("full_api_url", "").strip()
                        url = p.get("api_url", "").strip()
                        token = p.get("token", "").strip()
                        if not full_url and not url: continue
                        
                        urls_to_try = []
                        if full_url:
                            urls_to_try.append(full_url)
                        else:
                            if "{token}" in url or "{key}" in url:
                                urls_to_try.append(url.replace("{token}", token).replace("{key}", token))
                            elif "token=" in url or "key=" in url:
                                urls_to_try.append(url)
                            else:
                                sep = '&' if '?' in url else '?'
                                urls_to_try.append(f"{url}{sep}token={token}")
                                urls_to_try.append(f"{url}{sep}key={token}&start=0")
                                urls_to_try.append(f"{url}{sep}key={token}")
                            
                        parsed_data = []
                        # 🌟 Browser Bypass (403 Forbidden Fix)
                        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                        for try_url in urls_to_try:
                            try:
                                res = requests.get(try_url, headers=headers, timeout=10)
                                parsed_data = parse_panel_response(res.text, p)
                                if parsed_data:
                                    if not full_url and try_url != url and token:
                                        p["api_url"] = try_url.replace(token, "{token}")
                                        save_db()
                                    break
                            except: continue
                        if not parsed_data: continue
                    else:
                        continue
                    
                    if p.get("type") != "Auto Captcha Panel":
                        limit = p.get("records", 0)
                        if limit > 0: parsed_data = parsed_data[:limit]
                        
                    for item in parsed_data:
                        num = item["number"]
                        otp = item["otp"]
                        msg_text = item["message"]
                        if claim_processed_otp(num, otp):
                                 
                            char, iso = get_flag_and_code(num)
                            app_full_name, prem_app_html = get_service_info_html(p.get("name", "Panel"), msg_text)
                            current_time = time.time()
                            
                            # 🌟 শুধু লোকাল ফাইলে সেভ করবে!
                            save_db()
                                 
                            display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                            masked = mask_number(display_num)
                            lang = detect_language(msg_text)
                            
                            lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                            display_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
                            
                            for fw in bot_settings.get("fw_groups", []):
                                kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                                temp_row = []
                                styles = ["danger", "success", "primary"]
                                for i, btn in enumerate(fw.get("buttons", [])):
                                    b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                                    if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                                    temp_row.append(b_obj)
                                    if len(temp_row) == 2:
                                        kb.append(temp_row)
                                        temp_row = []
                                if temp_row: kb.append(temp_row)
                                send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                            
                            owners = []
                            clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()
                            
                            # 🌟 ALGORITHM FIX: সরাসরি Active Sessions থেকে মালিক খোঁজা 
                            # (কারণ Local Stock থেকে নাম্বার Assign হওয়ার সাথে সাথে ডিলিট হয়ে যায়)
                            for uid, session_data in list(user_active_sessions.items()):
                                for act_num in session_data.get("nums", []):
                                    act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                    if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                        owners.append(uid)
                                        break
                                        
                            # ব্যাকআপ হিসেবে StexSMS-তে চেক করা 
                            if not owners:
                                for stex_n, n_owner in stex_assigned_numbers.items():
                                    clean_stex = str(stex_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                    if clean_stex == clean_api_num or (len(clean_stex) >= 8 and clean_stex.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_stex[-8:])):
                                        owners.append(n_owner)
                                        
                            owners = list(set(owners)) 
                            for owner_id in owners:
                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                                
                                # রিওয়ার্ড যোগ করার লজিক
                                reward = float(bot_settings.get("otp_reward", 0.0))
                                if reward > 0:
                                    update_balance(owner_id, reward)
                                    inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                                
                                send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                                if db:
                                    try: 
                                        db.collection('users').document(str(owner_id)).update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1)})
                                        if owner_id in user_cache:
                                            user_cache[owner_id]["total_otps"] = user_cache[owner_id].get("total_otps", 0) + 1
                                    except: pass
        except Exception as e:
            pass
        time.sleep(5) 

# ==========================================
# Local User Management
# ==========================================
user_cache = {}

def default_user(user_id):
    return {
        "user_id": user_id,
        "balance": 0.0,
        "total_refers": 0,
        "total_otps": 0,
        "banned": False,
        "verified": False,
        "display_name": "User",
    }

def get_user(user_id):
    if user_id in user_cache: return user_cache[user_id]
    fallback = default_user(user_id)
    doc_ref = db.collection('users').document(str(user_id))
    try:
        doc = doc_ref.get()
        if doc.exists:
            data = {**fallback, **(doc.to_dict() or {})}
            user_cache[user_id] = data
            return data
        doc_ref.set(fallback)
    except Exception as e:
        print(f"Local user read/write deferred for {user_id}: {e}")
    user_cache[user_id] = fallback
    return fallback

def sync_user_display_name(chat_id, msg):
    # 🌟 Keeps each user's display name up to date (used by the public Leader Board).
    # Only writes when the name actually changes.
    try:
        frm = msg.get("from", {})
        full_name = f"{frm.get('first_name', '').strip()} {frm.get('last_name', '').strip()}".strip() or "User"
        u_data = get_user(chat_id)
        if u_data.get("display_name") != full_name:
            u_data["display_name"] = full_name
            user_cache[chat_id] = u_data
            try: db.collection('users').document(str(chat_id)).set({"display_name": full_name}, merge=True)
            except: pass
    except: pass

def update_balance(user_id, amount):
    user = get_user(user_id)
    user["balance"] = user.get("balance", 0.0) + float(amount)
    try:
        doc_ref = db.collection('users').document(str(user_id))
        doc_ref.set({"user_id": user_id, "balance": local_ops.Increment(float(amount))}, merge=True)
    except: pass

def add_referral(inviter_id, new_user_id):
    if not get_user(new_user_id).get("ref_paid", False):
        get_user(new_user_id) 
        reward = bot_settings.get("refer_reward", 0.2)
        update_balance(inviter_id, reward)
        db.collection('users').document(str(inviter_id)).update({"total_refers": local_ops.Increment(1)})
        if inviter_id in user_cache:
            user_cache[inviter_id]["total_refers"] = user_cache[inviter_id].get("total_refers", 0) + 1
        get_user(new_user_id)["ref_paid"] = True
        db.collection('users').document(str(new_user_id)).set({"ref_paid": True}, merge=True)
        
        # আপনার দেওয়া নতুন ডিজাইন
        ref_msg = (
            f"{PEM['gift']} <b>New Referral !</b>\n"
            f"------------------\n"
            f"🔥 <b>You Received {reward} TK</b>\n"
            f"------------------\n"
            f"{PEM['user']} <b>From User ID:</b> <code>{new_user_id}</code>"
        )
        send_message(inviter_id, render_body_text(ref_msg))

# ==========================================
# UI Keyboards & Menu Builders
# ==========================================
def get_cancel_kb():
    return {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_state", "style": "danger"}]]}

def main_menu(user_id):
    kb = [
        [
            {"text": "GET NUMBER", "icon_custom_emoji_id": "5337132498965010628", "style": "primary"}, 
            {"text": "2FA ONLINE", "icon_custom_emoji_id": "5267421176841398765", "style": "primary"}
        ],
        [
            {"text": "Refer", "icon_custom_emoji_id": "5420396762189831222", "style": "success"}, 
            {"text": "WITHDRAWAL", "icon_custom_emoji_id": "5352585194295564660", "style": "danger"}
        ],
        [
            {"text": "Leader Board", "icon_custom_emoji_id": "5440539497383087970", "style": "success"},
            {"text": "SUPPORT", "icon_custom_emoji_id": "5420145051336485498", "style": "primary"}
        ]
    ]
    # 🌟 FIX: STATUS is now visible to every user, not just admins - it shows a
    # rate card of the configured services, each with its country + OTP rate.
    # Admin Panel stays admin-only.
    if is_admin(user_id):
        kb.append([{"text": "STATUS", "icon_custom_emoji_id": "5352877703043258544", "style": "success"}, {"text": "Admin Panel", "icon_custom_emoji_id": "5420155432272438703", "style": "danger"}])
    else:
        kb.append([{"text": "STATUS", "icon_custom_emoji_id": "5352877703043258544", "style": "success"}])
    return {"keyboard": kb, "resize_keyboard": True}

def get_admin_text():
    users_count = len(all_known_users) # 🌟 Zero Cost User Count!
    total_files = len(number_batches)
    available_nums = sum(len(b["numbers"]) for b in number_batches.values())

    txt = f"""
{PEM['admin']} <b>ADMIN CONTROL PANEL</b> {PEM['admin']}
━━━━━━━━━━━━━━━━━━

{PEM['graph']} <b>DATABASE OVERVIEW</b>
— — — — — — — — — —
{PEM['user']} Users      » {users_count}
{PEM['file']} Files      » {total_files}
{PEM['num']} Numbers    » {total_uploaded_stats}
{PEM['ok']} Assigned   » {total_assigned_stats}
{PEM['rocket']} Available  » {available_nums}

{PEM['graph']} <b>STOCK LEVEL</b>
— — — — — — — — — —
[██████░░░░░░░░░] {available_nums} free
"""
    return render_body_text(txt)

def admin_panel_keyboard():
    return {"inline_keyboard": [
        [{"text": "LEADER BOARD SYSTEM", "icon_custom_emoji_id": "5353032893096567467", "callback_data": "lb_main", "style": "success"}],
        [{"text": "Upload Number", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "upload_num", "style": "primary"},
         {"text": "Delete files", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "delete_files", "style": "danger"}],
        [{"text": "Backup", "icon_custom_emoji_id": "5352597830089347330", "callback_data": "backup_menu", "style": "success"}],
        [{"text": "Broadcast", "icon_custom_emoji_id": "5789428375261023681", "callback_data": "broadcast_msg", "style": "success"},
         {"text": "System", "icon_custom_emoji_id": "5420155432272438703", "callback_data": "system_settings", "style": "primary"}],
        [{"text": "Used number", "icon_custom_emoji_id": "5352694861990501856", "callback_data": "show_used", "style": "success"},
         {"text": "Unused number", "icon_custom_emoji_id": "5352597830089347330", "callback_data": "show_unused", "style": "success"}],
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]
    ]}

def system_settings_keyboard():
    return {"inline_keyboard": [
        [{"text": "StexSMS Control", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "stex_control", "style": "success"},
         {"text": "Voltx Control", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "voltx_control", "style": "primary"}],
        [{"text": "Zebra Control", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "zebrasms_control", "style": "success"},
         {"text": "Yesms Control", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "yesms_control", "style": "primary"}],
        [{"text": "Force Join System", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "manage_fj", "style": "primary"},
         {"text": "Admin Management", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "manage_admins", "style": "danger"}],
        [{"text": "OTP Group", "icon_custom_emoji_id": "5190447043545438788", "callback_data": "manage_otp_groups", "style": "danger"},
         {"text": "User Management", "icon_custom_emoji_id": "5193063022226086560", "callback_data": "user_management", "style": "primary"}], 
         [{"text": "Panel MANAGEMENT", "icon_custom_emoji_id": "5336879280578138635", "callback_data": "manage_panels", "style": "danger"}],
        [{"text": "TGZ Control", "icon_custom_emoji_id": "5193100774988617665", "callback_data": "TGZ_control", "style": "primary"},
         {"text": "Premium Emoji", "icon_custom_emoji_id": "5352552689983067014", "callback_data": "manage_emojis", "style": "success"}],
        [{"text": "Menu Design", "icon_custom_emoji_id": "5190751148704833975", "callback_data": "menu_design_list", "style": "primary"},
         {"text": "Test", "icon_custom_emoji_id": "5190781475468915802", "callback_data": "test_message_flow", "style": "primary"}], 
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
    ]}

def get_user_management_text():
    # 🌟 Fast & Free User Management Stats!
    total = len(all_known_users)
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    txt = f"""➖➖➖➖➖➖➖➖
《 👋 USER VIEW 》
➖➖➖➖➖➖➖➖
📊 LIVE STATISTICS:
➖➖➖➖➖➖➖➖
🫂 TOTAL USERS: {total}
✅ VERIFIED USERS: (Hidden to save DB Cost)
🚫 BANNED USERS: (Hidden to save DB Cost)
➖➖➖➖➖➖➖➖
⌛ UPDATED: {now_str}"""
    return render_body_text(txt)

def user_management_keyboard():
    return {"inline_keyboard": [
        [{"text": "Manage Balance", "icon_custom_emoji_id": "5190576863226933563", "callback_data": "um_manage_balance", "style": "primary"},
         {"text": "Ban/Unban User", "icon_custom_emoji_id": "5334807341109908955", "callback_data": "um_ban_unban", "style": "danger"}],
        [{"text": "User Profile", "icon_custom_emoji_id": "5352861489541714456", "callback_data": "um_user_profile", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def menu_design_list_keyboard():
    return {"inline_keyboard": [
        [{"text": "Edit /start Menu", "icon_custom_emoji_id": "5395444784611480792", "callback_data": "md_edit_start", "style": "primary"}],
        [{"text": "Edit GET NUMBER", "icon_custom_emoji_id": "5337132498965010628", "callback_data": "md_edit_get_number", "style": "success"},
         {"text": "Edit Select Country", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "md_edit_select_country", "style": "primary"}],
        [{"text": "Edit Refer", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "md_edit_refer", "style": "primary"}],
        [{"text": "Edit WITHDRAWAL", "icon_custom_emoji_id": "5352585194295564660", "callback_data": "md_edit_withdrawal", "style": "danger"},
         {"text": "Edit SUPPORT", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "md_edit_support", "style": "danger"}],
        [{"text": "Reset Defaults", "icon_custom_emoji_id": "5192812028632274956", "callback_data": "md_reset_defaults", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def menu_edit_options_keyboard(menu_key):
    return {"inline_keyboard": [
        [{"text": "Edit Body (Text)", "icon_custom_emoji_id": "5395444784611480792", "callback_data": f"md_text_{menu_key}", "style": "primary"}],
        [{"text": "Edit Inline Buttons", "icon_custom_emoji_id": "5420155432272438703", "callback_data": f"md_btns_{menu_key}", "style": "success"}],
        [{"text": "Back to Menus", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "menu_design_list", "style": "danger"}]
    ]}

def menu_buttons_list_keyboard(menu_key):
    kb = []
    btns = bot_settings["custom_messages"].get(menu_key, {}).get("buttons", [])
    for idx, btn in enumerate(btns):
        kb.append([{"text": f"Del: {btn['text']}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"md_delbtn_{menu_key}_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Inline Button", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"md_addbtn_{menu_key}", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"md_edit_{menu_key}", "style": "primary"}])
    return {"inline_keyboard": kb}

def emoji_settings_keyboard():
    return {"inline_keyboard": [
        [{"text": "Upload Flags (TXT)", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "up_flags_txt", "style": "primary"},
         {"text": "Download Flags", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_flags_txt", "style": "success"}],
        [{"text": "Upload Services (TXT)", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "up_apps_txt", "style": "primary"},
         {"text": "Download Services", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_apps_txt", "style": "success"}],
        [{"text": "Delete All Flags", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "del_all_flags", "style": "danger"},
         {"text": "Add Single Emoji", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_single_emoji", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]
    ]}

def status_services_keyboard():
    kb = []
    for idx, srv in enumerate(bot_settings.get("status_services", [])):
        kb.append([{"text": f"Delete {srv}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"status_del_srv_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "status_add_srv", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}])
    return {"inline_keyboard": kb}

def collect_status_rows(service_name):
    """For one watched service, find every country + OTP rate configured for
    it across all four panels (StexSMS/Voltx/Zebrasms/Yesms), deduplicated."""
    srv_upper = service_name.strip().upper()
    rows = []
    seen_countries = set()
    for provider in ("stex", "voltx", "zebrasms", "yesms"):
        countries = bot_settings.get(f"{provider}_services", {}).get(srv_upper, {})
        rates = bot_settings.get(f"{provider}_service_rates", {}).get(srv_upper, {})
        for country in countries:
            if country in seen_countries:
                continue
            seen_countries.add(country)
            rate = rates.get(country, bot_settings.get("otp_reward", 0.0))
            rows.append((country, rate))
    return rows

def get_all_configured_services():
    """Union of every service name that has country/rate data configured in
    any of the four panels (StexSMS/Voltx/Zebrasms/Yesms)."""
    services = set()
    for provider in ("stex", "voltx", "zebrasms", "yesms"):
        services.update(bot_settings.get(f"{provider}_services", {}).keys())
    return services

def build_status_report_text():
    apps = bot_settings.get("premium_apps", {})
    # 🌟 FIX: previously ONLY services manually added via "STATUS Services"
    # were ever shown, so a service that was fully configured with country +
    # OTP rate in the panels but never separately whitelisted here simply
    # never appeared. Now the curated list (admin-chosen order) is shown
    # first, and any other configured service is added automatically.
    curated = bot_settings.get("status_services", [])
    seen = set()
    services = []
    for srv in curated:
        key = srv.strip().upper()
        if key not in seen:
            seen.add(key)
            services.append(srv)
    for key in sorted(get_all_configured_services()):
        if key not in seen:
            seen.add(key)
            services.append(key)
    blocks = []
    for srv in services:
        srv_key = srv.strip().upper()
        app_data = apps.get(srv_key, {})
        icon = app_data.get("char", "📱")
        eid = app_data.get("id")
        app_html = f'<tg-emoji emoji-id="{eid}">{icon}</tg-emoji>' if eid else icon
        rows = collect_status_rows(srv)
        if not rows:
            continue
        lines = [f"{app_html} <b>{html.escape(srv)}</b> \""]
        for country, rate in rows:
            flag_char, flag_id = get_country_flag_info(country)
            flag_html = f'<tg-emoji emoji-id="{flag_id}">{flag_char}</tg-emoji>' if flag_id else flag_char
            cname = country.split(" (")[0].upper()
            lines.append(f"├ {flag_html} {html.escape(cname)} — {rate}৳")
        blocks.append("\n".join(lines))
    if not blocks:
        body = "└ <i>No services added yet.</i>"
    else:
        body = "\n━━━━━━━━━━━━━━━\n".join(blocks)
    date_str = datetime.now().strftime("%m/%d/%Y")
    return f"{body}\n━━━━━━━━━━━━━━━\n📅 Date: {date_str}"

def status_kb():
    return {"inline_keyboard": [
        [{"text": "Refresh", "icon_custom_emoji_id": "5375338737028841420", "callback_data": "status_refresh", "style": "primary"}],
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "status_close", "style": "danger"}]
    ]}

def fj_settings_keyboard():
    status_text = 'ON' if bot_settings['fj_on'] else 'OFF'
    status_icon = "5352694861990501856" if bot_settings['fj_on'] else "5318840353510408444"
    kb = [[{"text": f"STATUS: {status_text}", "icon_custom_emoji_id": status_icon, "callback_data": "toggle_fj", "style": "primary"}]]
    for idx, ch in enumerate(bot_settings["fj_channels"]):
        kb.append([{"text": f"Delete: {ch}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_fj_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Channel", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_fj", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}])
    return {"inline_keyboard": kb}

def admin_settings_keyboard():
    kb = []
    for idx, adm in enumerate(bot_settings["admins"]):
        text_btn = f"Owner: {adm}" if adm == OWNER_ID else f"Delete: {adm}"
        icon_id = "5353032893096567467" if adm == OWNER_ID else "5420130255174145507"
        cb_data = "ignore" if adm == OWNER_ID else f"del_adm_{idx}"
        kb.append([{"text": text_btn, "icon_custom_emoji_id": icon_id, "callback_data": cb_data, "style": "danger" if adm != OWNER_ID else "primary"}])
    kb.append([{"text": "Add Admin", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_adm", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}])
    return {"inline_keyboard": kb}

def otp_groups_list_keyboard():
    kb = [[{"text": "Edit OTP Button Link", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "edit_otp_link", "style": "primary"}]]
    for idx, fg in enumerate(bot_settings["fw_groups"]):
        kb.append([{"text": f"Group: {fg['chat_id']}", "icon_custom_emoji_id": "5193063022226086560", "callback_data": f"manage_fw_{idx}", "style": "primary"}])
    kb.append([{"text": "Add Forward Group", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_fw", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}])
    return {"inline_keyboard": kb}

def stex_control_keyboard():
    return {"inline_keyboard": [
        [{"text": "Add StexSMS Key", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_stex_key", "style": "success"},
         {"text": "View/Del Keys", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "view_stex_keys", "style": "danger"}],
        [{"text": "Manage StexSMS Services", "icon_custom_emoji_id": "5192739271886282680", "callback_data": "manage_stex_srv", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def zebrasms_control_keyboard():
    return {"inline_keyboard": [
        [{"text": "Add Zebra Key", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_zebrasms_key", "style": "success"},
         {"text": "View/Del Keys", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "view_zebrasms_keys", "style": "danger"}],
        [{"text": "Manage Zebra Services", "icon_custom_emoji_id": "5192739271886282680", "callback_data": "manage_zebrasms_srv", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def voltx_control_keyboard():
    return {"inline_keyboard": [
        [{"text": "Add Voltx Key", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_voltx_key", "style": "success"},
         {"text": "View/Del Keys", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "view_voltx_keys", "style": "danger"}],
        [{"text": "Manage Voltx Services", "icon_custom_emoji_id": "5192739271886282680", "callback_data": "manage_voltx_srv", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def yesms_control_keyboard():
    return {"inline_keyboard": [
        [{"text": "Add Yesms Key", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_yesms_key", "style": "success"},
         {"text": "View/Del Keys", "icon_custom_emoji_id": "5422557736330106570", "callback_data": "view_yesms_keys", "style": "danger"}],
        [{"text": "Manage Yesms Services", "icon_custom_emoji_id": "5192739271886282680", "callback_data": "manage_yesms_srv", "style": "success"}],
        [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "primary"}]
    ]}

def specific_fw_group_keyboard(idx):
    group = bot_settings["fw_groups"][idx]
    kb = []
    for b_idx, btn in enumerate(group.get("buttons", [])):
        kb.append([{"text": f"Del: {btn['text']}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_fwbtn_{idx}_{b_idx}", "style": "danger"}])
    
    kb.append([{"text": "Add Inline Button", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"add_fwbtn_{idx}", "style": "success"}])
    kb.append([{"text": "Delete Entire Group", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"del_fw_{idx}", "style": "danger"}])
    kb.append([{"text": "Back to Groups", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_otp_groups", "style": "primary"}])
    return {"inline_keyboard": kb}

def TGZ_control_keyboard():
    w_status = "ON" if bot_settings["withdraw_on"] else "OFF"
    sup_status = "ON" if bot_settings.get("support_link") else "OFF"
    grp_status = "ON" if bot_settings.get("w_group") else "OFF"
    return {"inline_keyboard": [
        [{"text": f"WITHDRAW: {w_status}", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "TGZ_toggle_w", "style": "primary"}],
        [{"text": f"MIN WITHDRAW: {bot_settings['min_withdraw']}", "icon_custom_emoji_id": "5352877703043258544", "callback_data": "TGZ_min_w", "style": "success"},
         {"text": f"OTP REWARD: {bot_settings['otp_reward']}", "icon_custom_emoji_id": "5190576863226933563", "callback_data": "TGZ_otp_r", "style": "primary"}],
        [{"text": f"REFER REWARD: {bot_settings['refer_reward']}", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "TGZ_ref_r", "style": "success"},
         {"text": f"COOLDOWN: {bot_settings['cooldown']}s", "icon_custom_emoji_id": "5337172996211648018", "callback_data": "TGZ_cool", "style": "primary"}],
        [{"text": f"NUM/REQ: {bot_settings['num_req']}", "icon_custom_emoji_id": "5337132498965010628", "callback_data": "TGZ_num_req", "style": "success"},
         {"text": f"NUM/SHARE: {bot_settings['num_share']}", "icon_custom_emoji_id": "5352862640592949843", "callback_data": "TGZ_num_share", "style": "primary"}],
        [{"text": f"SUPPORT LINK: {sup_status}", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "TGZ_sup_link", "style": "success"},
         {"text": "W. METHODS", "icon_custom_emoji_id": "5190899075968441286", "callback_data": "manage_w_methods", "style": "primary"}],
        [{"text": f"W. GROUP: {grp_status}", "icon_custom_emoji_id": "5420517437885943844", "callback_data": "TGZ_w_group", "style": "success"}],
        [{"text": "BACK", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]
    ]}

def w_methods_keyboard():
    kb = []
    for idx, m in enumerate(bot_settings["w_methods"]):
        kb.append([{"text": f"Delete: {m}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_wm_{idx}", "style": "danger"}])
    kb.append([{"text": "Add Method", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "add_wm", "style": "success"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "TGZ_control", "style": "primary"}])
    return {"inline_keyboard": kb}

def typed_panels_list_keyboard(p_type):
    kb = []
    for idx, p in enumerate(bot_settings["panels"]):
        if p.get("type", "API Panel") != p_type: continue
        action_text = f"Turn OFF {p['name']}" if p['status'] == 'ON' else f"Turn ON {p['name']}"
        action_icon = "5318840353510408444" if p['status'] == 'ON' else "5192812028632274956"
        icon_id = "5420155432272438703" 
        kb.append([
            {"text": action_text, "icon_custom_emoji_id": action_icon, "callback_data": f"tog_pnl_{idx}", "style": "danger" if p['status'] == 'ON' else "success"},
            {"text": f"{p['name']}", "icon_custom_emoji_id": icon_id, "callback_data": f"conf_pnl_{idx}", "style": "primary"}
        ])
    add_cb = "add_api_panel" if p_type == "API Panel" else "add_cpt_panel"
    kb.append([{"text": "Add New Provider", "icon_custom_emoji_id": "5420323438508155202", "callback_data": add_cb, "style": "success"}])
    kb.append([{"text": "Delete Provider", "icon_custom_emoji_id": "5336944168944047463", "callback_data": f"list_del_{'api' if p_type=='API Panel' else 'cpt'}", "style": "danger"}])
    kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_panels", "style": "primary"}])
    return {"inline_keyboard": kb}

def panel_config_keyboard(idx):
    p = bot_settings["panels"][idx]
    
    kb = []
    action_text = "Turn OFF" if p['status'] == 'ON' else "Turn ON"
    action_icon = "5318840353510408444" if p['status'] == 'ON' else "5192812028632274956"
    kb.append([{"text": action_text, "icon_custom_emoji_id": action_icon, "callback_data": f"tog_pnl_{idx}", "style": "danger" if p['status'] == 'ON' else "success"}])
    
    if p["type"] != "Auto Captcha Panel":
        rec_count_text = "All (Unlimited)" if p.get('records', 0) == 0 else str(p.get('records'))
        kb.append([{"text": "Set API URL", "icon_custom_emoji_id": "5420517437885943844", "callback_data": f"set_p_api_{idx}", "style": "primary"}])
        kb.append([{"text": "Set Token", "icon_custom_emoji_id": "5353022963132174959", "callback_data": f"set_p_tok_{idx}", "style": "primary"}])
        kb.append([{"text": "🌐 Full API (URL+Token)", "icon_custom_emoji_id": "5420517437885943844", "callback_data": f"set_p_fapi_{idx}", "style": "primary"}])
        kb.append([{"text": f"Set Records Count: {rec_count_text}", "icon_custom_emoji_id": "5192739271886282680", "callback_data": f"set_p_rec_{idx}", "style": "primary"}])
        
    kb.append([{"text": "Test Connection", "icon_custom_emoji_id": "5352694861990501856", "callback_data": f"test_p_conn_{idx}", "style": "success"}])
        
    back_data = "manage_api_panels" if p.get("type", "API Panel") == "API Panel" else "manage_cpt_panels"
    kb.append([{"text": "Back to Providers", "icon_custom_emoji_id": "5267490665117275176", "callback_data": back_data, "style": "danger"}])
    return {"inline_keyboard": kb}

# ==========================================
# Message Handler
# ==========================================
def handle_message(msg):
    global total_uploaded_stats
    chat_id = msg["chat"]["id"]
    chat_type = msg["chat"].get("type", "private")
    
    if chat_type != "private":
        return
        
    text = msg.get("text", "")
    register_user_local(chat_id) # 🌟 Save User locally for Free Broadcasts!
    sync_user_display_name(chat_id, msg) # 🌟 Keep name fresh for the Leader Board

    if is_user_banned(chat_id):
        send_message(chat_id, render_body_text("🚫 <b>You are banned from using this bot!</b>\nIf you think this is a mistake, please contact support."))
        return
    
    # --- REFERRAL FIX: Save inviter BEFORE Force Join ---
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1 and parts[1].isdigit():
            inviter = int(parts[1])
            if inviter != chat_id:
                if db:
                    try:
                        doc = db.collection('users').document(str(chat_id)).get()
                        if not doc.exists:
                            get_user(chat_id)
                            db.collection('users').document(str(chat_id)).set({"referred_by": inviter, "ref_paid": False}, merge=True)
                    except Exception as e:
                            print(f"Local referral setup deferred for {chat_id}: {e}")
                        
    if not check_force_join(chat_id):
        send_force_join_msg(chat_id)
        return
        
    MAIN_MENU_CMDS = ["GET NUMBER", "Refer", "WITHDRAWAL", "SUPPORT", "Admin Panel", "2FA ONLINE", "STATUS", "Leader Board"]
    
    is_main_cmd = False
    if text in MAIN_MENU_CMDS or text.startswith("/start"):
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        is_main_cmd = True
    
    if chat_id in user_states and not is_main_cmd:
        state = user_states[chat_id]
        
        # 🌟 Auto Captcha Panel Setup Flow 
        if state == "wait_for_cpanel_url" and text:
            temp_data[chat_id]["p_data"]["login_url"] = text.strip()
            user_states[chat_id] = "wait_for_cpanel_user"
            send_message(chat_id, render_body_text("2️⃣ <b>Username</b>\n➡️ Panel এর Username দিন:"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_user" and text:
            temp_data[chat_id]["p_data"]["username"] = text.strip()
            user_states[chat_id] = "wait_for_cpanel_pass"
            send_message(chat_id, render_body_text("3️⃣ <b>Password</b>\n➡️ Panel এর Password দিন:"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_pass" and text:
            temp_data[chat_id]["p_data"]["password"] = text.strip()
            user_states[chat_id] = "wait_for_cpanel_msg_link"
            send_message(chat_id, render_body_text("4️⃣ <b>Message Link</b>\n➡️ যেখান থেকে SMS/OTP ডাটা (JSON) আসবে সেই Link দিন:"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_msg_link" and text:
            temp_data[chat_id]["p_data"]["msg_link"] = text.strip()
            user_states[chat_id] = "wait_for_cpanel_num_col_name"
            send_message(chat_id, render_body_text("5️⃣ <b>Number Column Name</b>\n➡️ Data তে Number column এর নাম কী? (যেমন: number, phone):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_num_col_name" and text:
            temp_data[chat_id]["p_data"]["num_col_name"] = text.strip()
            user_states[chat_id] = "wait_for_cpanel_num_col_idx"
            send_message(chat_id, render_body_text("6️⃣ <b>Number Column Serial</b>\n➡️ Number Column এর Serial Number কত? (যেমন: 3, 5):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_num_col_idx" and text:
            if text.isdigit():
                temp_data[chat_id]["p_data"]["num_col_idx"] = int(text)
                user_states[chat_id] = "wait_for_cpanel_msg_col_name"
                send_message(chat_id, render_body_text("7️⃣ <b>Message Column Name</b>\n➡️ Message/OTP column এর নাম কী? (যেমন: message, sms):"), reply_markup=get_cancel_kb())
            else:
                 send_message(chat_id, render_body_text("❌ Please enter a valid number serial!"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_msg_col_name" and text:
            temp_data[chat_id]["p_data"]["msg_col_name"] = text.strip()
            user_states[chat_id] = "wait_for_cpanel_msg_col_idx"
            send_message(chat_id, render_body_text("8️⃣ <b>Message Column Serial</b>\n➡️ Message Column এর Serial Number কত? (যেমন: 5, 7):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_cpanel_msg_col_idx" and text:
            if text.isdigit():
                temp_data[chat_id]["p_data"]["msg_col_idx"] = int(text)
                temp_data[chat_id]["p_data"]["login_status"] = "⏳ Pending Auto-Login..."
                
                # Save the panel configuration
                bot_settings["panels"].append(temp_data[chat_id]["p_data"])
                save_db()
                
                send_message(chat_id, render_body_text(f"{PEM['ok']} <b>Auto Captcha Panel Added Successfully!</b>\nবট এখন থেকে নিজেই ব্যাকগ্রাউন্ডে ক্যাপচা সলভ করে লগিন করে নিবে।"), reply_markup=main_menu(chat_id))
                
                msg_id = temp_data[chat_id]["msg_id"]
                handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_cpt_panels", "id": "internal"})
                
                del user_states[chat_id]
                del temp_data[chat_id]
            else:
                 send_message(chat_id, render_body_text("❌ Please enter a valid number serial!"), reply_markup=get_cancel_kb())
            return

        # --- User Management Flows ---
        elif state == "wait_for_um_bal_uid" and text:
            target_uid_str = text.strip()
            if not target_uid_str.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID! Please send a numeric User ID."), reply_markup=get_cancel_kb())
                return
            target_uid = int(target_uid_str)
            if db:
                doc = db.collection('users').document(str(target_uid)).get()
                if not doc.exists:
                    send_message(chat_id, render_body_text("❌ User not found in database!"), reply_markup=get_cancel_kb())
                    return
                current_bal = doc.to_dict().get('balance', 0.0)
                temp_data[chat_id]["target_uid"] = target_uid
                user_states[chat_id] = "wait_for_um_bal_amt"
                send_message(chat_id, render_body_text(f"✅ User found!\n💰 Current Balance: {current_bal} ৳\n\n📝 Send the amount to ADD (e.g. 50) or REMOVE (e.g. -50):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_um_bal_amt" and text:
            try:
                amt = float(text.strip())
                target_uid = temp_data[chat_id]["target_uid"]
                update_balance(target_uid, amt)
                send_message(chat_id, render_body_text(f"{PEM['ok']} Balance updated successfully for {target_uid}!"), reply_markup=main_menu(chat_id))
                send_message(target_uid, render_body_text(f"🔔 Your balance has been adjusted by <b>{amt} ৳</b> by an Admin."))
                del user_states[chat_id]
                del temp_data[chat_id]
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid amount! Please send a number."), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_um_ban_uid" and text:
            target_uid_str = text.strip()
            if not target_uid_str.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID!"), reply_markup=get_cancel_kb())
                return
            target_uid = int(target_uid_str)
            if db:
                doc_ref = db.collection('users').document(str(target_uid))
                doc = doc_ref.get()
                if not doc.exists:
                    send_message(chat_id, render_body_text("❌ User not found in database!"), reply_markup=get_cancel_kb())
                    return
                current_status = doc.to_dict().get("banned", False)
                new_status = not current_status
                doc_ref.update({"banned": new_status})
                
                user_banned_cache[target_uid] = {'banned': new_status, 'time': time.time()}
                
                status_text = "BANNED 🚫" if new_status else "UNBANNED ✅"
                send_message(chat_id, render_body_text(f"✅ User {target_uid} has been {status_text}!"), reply_markup=main_menu(chat_id))
                del user_states[chat_id]
                del temp_data[chat_id]
            return

        elif state == "wait_for_um_prof_uid" and text:
            target_uid_str = text.strip()
            if not target_uid_str.isdigit():
                send_message(chat_id, render_body_text("❌ Invalid ID!"), reply_markup=get_cancel_kb())
                return
            target_uid = int(target_uid_str)
            if db:
                doc = db.collection('users').document(str(target_uid)).get()
                if not doc.exists:
                    send_message(chat_id, render_body_text("❌ User not found in database!"), reply_markup=get_cancel_kb())
                    return
                data = doc.to_dict()
                is_verified = True if data.get('total_otps', 0) > 0 else data.get('verified', False)
                prof_text = f"""➖➖➖➖➖➖➖➖
👤 <b>USER PROFILE</b>
➖➖➖➖➖➖➖➖
🆔 ID: <code>{target_uid}</code>
💰 Balance: {data.get('balance', 0.0)} ৳
🤝 Total Refers: {data.get('total_refers', 0)}
🔐 Total OTPs: {data.get('total_otps', 0)}
✅ Verified: {is_verified}
🚫 Banned: {data.get('banned', False)}
➖➖➖➖➖➖➖➖"""
                kb = {"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "user_management", "style": "primary"}]]}
                send_message(chat_id, render_body_text(prof_text), reply_markup=kb)
                del user_states[chat_id]
                del temp_data[chat_id]
            return

        # --- Menu Design Flow ---
        elif state == "wait_for_menu_text" and text:
            try:
                menu_key = temp_data[chat_id]["menu_key"]
                formatted_html_text = extract_premium_html(msg)
                
                bot_settings["custom_messages"][menu_key]["text"] = formatted_html_text
                save_db()
                
                delete_message(chat_id, msg["message_id"])
                
                preview_text = render_body_text(formatted_html_text)
                success_text = f"{PEM['ok']} <b>Message Body Updated successfully!</b>\n\n🎨 <b>Editing: {menu_key.upper()}</b>\n\nPreview of current Text:\n{preview_text}"
                edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(success_text), reply_markup=menu_edit_options_keyboard(menu_key))
            except Exception as e:
                send_message(chat_id, f"❌ Error saving text: {e}")
            finally:
                if chat_id in user_states: del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
            return
            
        elif state == "wait_for_menu_btn" and text:
            try:
                menu_key = temp_data[chat_id]["menu_key"]
                if "-" in text:
                    parts = text.split("-", 1)
                    btn_text = parts[0].strip()
                    btn_url = parts[1].strip()
                    
                    emoji_id = None
                    emoji_char = ""
                    for ent in msg.get("entities", []):
                        if ent.get("type") == "custom_emoji":
                            emoji_id = ent.get("custom_emoji_id")
                            offset = ent.get("offset", 0)
                            length = ent.get("length", 0)
                            b_text = text.encode('utf-16-le')
                            emoji_char = b_text[offset*2:(offset+length)*2].decode('utf-16-le')
                            break
                            
                    if emoji_char:
                        btn_text = btn_text.replace(emoji_char, "").strip()
                        
                    btn_data = {"text": btn_text, "url": btn_url, "style": "primary"}
                    if emoji_id:
                        btn_data["icon_custom_emoji_id"] = emoji_id
                        
                    bot_settings["custom_messages"][menu_key]["buttons"].append(btn_data)
                    save_db()
                    delete_message(chat_id, msg["message_id"])
                    edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"{PEM['gear']} <b>Edit Inline Buttons: {menu_key.upper()}</b>"), reply_markup=menu_buttons_list_keyboard(menu_key))
                else:
                    send_message(chat_id, render_body_text(f"{PEM['no']} Invalid format. Use <code>Button Text - https://link.com</code>"))
            except Exception as e:
                 pass
            finally:
                if chat_id in user_states: del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
            return

        elif state == "wait_for_test_service" and text:
            temp_data[chat_id]["service"] = text.strip()
            user_states[chat_id] = "wait_for_test_number"
            send_message(chat_id, render_body_text("📝 Send the Number (e.g. +8801712345678):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_test_number" and text:
            temp_data[chat_id]["number"] = text.strip()
            user_states[chat_id] = "wait_for_test_otp"
            send_message(chat_id, render_body_text("📝 Send the OTP (e.g. 556677):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_test_otp" and text:
            temp_data[chat_id]["otp"] = text.strip()
            user_states[chat_id] = "wait_for_test_lang"
            send_message(chat_id, render_body_text("📝 Send the Language (e.g. EN, AR):"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_test_lang" and text:
            lang = text.strip().upper()
            if not lang.startswith("#"):
                lang = "#" + lang
                
            srv = temp_data[chat_id]["service"]
            num = temp_data[chat_id]["number"]
            otp = temp_data[chat_id]["otp"]
            
            masked = mask_number(num)
            prem_flag_html = get_flag_info_html(num)
            char, iso = get_flag_and_code(num)
            app_full_name, prem_app_html = get_service_info_html(srv)
            
            lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
            msg_text = render_body_text(f"{prem_flag_html} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
            
            for fw in bot_settings.get("fw_groups", []):
                kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                temp_row = []
                styles = ["danger", "success", "primary"]
                for i, btn in enumerate(fw.get("buttons", [])):
                    b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                    if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                    temp_row.append(b_obj)
                    if len(temp_row) == 2:
                        kb.append(temp_row)
                        temp_row = []
                if temp_row: kb.append(temp_row)
                send_message(fw["chat_id"], msg_text, reply_markup={"inline_keyboard": kb})
                
            send_message(chat_id, render_body_text(f"{PEM['ok']} Test message formatted and sent to all Forward Groups!"), reply_markup=main_menu(chat_id))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_emoji_extract":
            entities = msg.get("entities", [])
            custom_emoji_id = None
            emoji_text = ""
            for ent in entities:
                if ent.get("type") == "custom_emoji":
                    custom_emoji_id = ent.get("custom_emoji_id")
                    offset = ent.get("offset", 0)
                    length = ent.get("length", 0)
                    b_text = msg.get("text", "").encode('utf-16-le')
                    emoji_text = b_text[offset*2:(offset+length)*2].decode('utf-16-le')
                    break
            
            if custom_emoji_id:
                temp_data[chat_id] = {"id": custom_emoji_id, "char": emoji_text}
                user_states[chat_id] = "wait_for_emoji_details"
                send_message(chat_id, render_body_text(f"{PEM['ok']} Emoji ID পাওয়া গেছে: <code>{custom_emoji_id}</code>\n\n📌 এখন এটি সেভ করার জন্য টাইপ এবং নাম লিখুন।\n\n<b>ফরমেট:</b>\n`FLAG | 880 | BD | Bangladesh`\nঅথবা\n`APP | WhatsApp`"), reply_markup=get_cancel_kb())
            else:
                send_message(chat_id, render_body_text(f"{PEM['no']} কোনো Premium Emoji পাওয়া যায়নি! দয়া করে Custom Emoji সেন্ড করুন।"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_emoji_details" and text:
            parts = [p.strip() for p in text.split("|")]
            mode = parts[0].upper()
            eid = temp_data[chat_id]["id"]
            char = temp_data[chat_id]["char"]
            
            if mode == "FLAG" and len(parts) == 4:
                code, iso, name = parts[1], parts[2], parts[3]
                bot_settings["premium_flags"][code] = {"char": char, "iso": iso.upper(), "name": name, "id": eid}
                save_db()
                send_message(chat_id, render_body_text(f"{PEM['ok']} Flag Emoji সেভ হয়েছে!\nCode: {code} | Name: {name}"), reply_markup=emoji_settings_keyboard())
            elif mode == "APP" and len(parts) == 2:
                name = parts[1]
                bot_settings["premium_apps"][name.upper()] = {"char": char, "id": eid, "name": name.title()}
                save_db()
                send_message(chat_id, render_body_text(f"{PEM['ok']} App Emoji সেভ হয়েছে!\nName: {name}"), reply_markup=emoji_settings_keyboard())
            else:
                send_message(chat_id, render_body_text(f"{PEM['no']} ফরম্যাট ভুল!\n\nসঠিক ফরম্যাট:\n`FLAG | 880 | BD | Bangladesh`\n`APP | WhatsApp`"))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state in ["wait_for_flag_txt", "wait_for_app_txt"] and "document" in msg:
            doc = msg["document"]
            if not doc["file_name"].endswith(".txt"):
                send_message(chat_id, render_body_text(f"{PEM['no']} Please upload a .txt file only."))
                return
            file_id = doc["file_id"]
            file_info = requests.get(
                f"{BASE_URL}/getFile?file_id={file_id}", timeout=(10, 20)
            ).json()
            file_path = file_info["result"]["file_path"]
            content = requests.get(f"{FILE_URL}{file_path}", timeout=(10, 30)).text
            
            mode = "flags" if state == "wait_for_flag_txt" else "apps"
            count = 0
            
            if mode == "flags":
                for line in content.splitlines():
                    json_match = re.search(r'(\{.*\})', line)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                            char = data.get("emoji")
                            eid = data.get("id")
                            
                            prefix_str = line[:json_match.start()].strip()
                            code_match = re.search(r'\((\d+)\)', prefix_str)
                            iso_match = re.search(r'\(([A-Za-z]+)\)', prefix_str)
                            
                            if code_match and iso_match and char and eid:
                                code = code_match.group(1)
                                iso = iso_match.group(1).upper()
                                name = prefix_str.replace(f"({code})", "").replace(f"({iso_match.group(1)})", "").replace(char, "").strip()
                                bot_settings["premium_flags"][code] = {"char": char, "iso": iso, "name": name, "id": eid}
                                count += 1
                        except: pass
            else:
                for line in content.splitlines():
                    json_match = re.search(r'(\{.*\})', line)
                    if json_match:
                        try:
                            data = json.loads(json_match.group(1))
                            char = data.get("emoji")
                            eid = data.get("id")
                            
                            name_part = line[:json_match.start()].strip()
                            name = name_part.replace(char, '').strip() if char else name_part
                            
                            if char and eid and name:
                                bot_settings["premium_apps"][name.upper()] = {"char": char, "id": eid, "name": name}
                                count += 1
                        except: pass
            
            save_db()
            send_message(chat_id, render_body_text(f"{PEM['ok']} Successfully loaded {count} Emojis!"), reply_markup=emoji_settings_keyboard())
            del user_states[chat_id]
            return

        elif state == "wait_for_broadcast":
            msg_id = msg["message_id"]
            send_message(chat_id, render_body_text(f"{PEM['ok']} Broadcast started..."))
            threading.Thread(target=broadcast_copymessage, args=(chat_id, msg_id)).start()
            del user_states[chat_id]
            return

        elif state == "wait_for_backup_upload" and "document" in msg:
            doc = msg["document"]
            filename = doc.get("file_name", "").lower()
            if not filename.endswith(".json"):
                send_message(chat_id, render_body_text(f"{PEM['no']} Please upload a .json backup file only."))
                return
            try:
                file_info = requests.get(
                    f"{BASE_URL}/getFile?file_id={doc['file_id']}", timeout=(10, 20)
                ).json()
                file_path = file_info["result"]["file_path"]
                raw_backup = requests.get(f"{FILE_URL}{file_path}", timeout=(10, 30)).content
                # Validate and replace atomically through the local database layer.
                db.import_bytes(raw_backup)
                load_db()
                user_cache.clear()
                all_known_users.clear()
                sync_users_list()
                user_banned_cache.clear()
                del user_states[chat_id]
                send_message(chat_id, render_body_text(
                    f"{PEM['ok']} <b>Backup restored successfully!</b>\n"
                    "All settings, users, numbers, balances and withdrawal history are now loaded."
                ), reply_markup=admin_panel_keyboard())
            except Exception as exc:
                print(f"Backup restore error: {exc}")
                send_message(chat_id, render_body_text(
                    f"{PEM['no']} Backup restore failed. The existing data was kept.\n<code>{html.escape(str(exc))}</code>"
                ), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_txt" and "document" in msg:
            doc = msg["document"]
            if not doc["file_name"].endswith(".txt"):
                send_message(chat_id, render_body_text(f"{PEM['no']} Please upload a .txt file only."))
                return
            file_id = doc["file_id"]
            file_info = requests.get(
                f"{BASE_URL}/getFile?file_id={file_id}", timeout=(10, 20)
            ).json()
            file_path = file_info["result"]["file_path"]
            file_content = requests.get(f"{FILE_URL}{file_path}", timeout=(10, 30)).text
            
            temp_data[chat_id] = {"numbers": file_content.splitlines(), "filename": doc["file_name"]}
            user_states[chat_id] = "wait_for_service"
            send_message(chat_id, render_body_text(f"{PEM['ok']} File received.\n\n📌 Enter the service name (e.g., WHATSAPP):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_service" and text:
            temp_data[chat_id]["service"] = text.upper()
            user_states[chat_id] = "wait_for_country"
            send_message(chat_id, render_body_text(f"{PEM['ok']} Service set.\n\n{PEM['world']} Enter the country name (e.g., YEMEN):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_country" and text:
            country = text.upper()
            temp_data[chat_id]["country"] = country
            user_states[chat_id] = "wait_for_file_rate"
            send_message(chat_id, render_body_text(f"{PEM['ok']} Country set.\n\n{PEM['money']} Enter the OTP reward rate for this file (e.g. 5.0):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_for_file_rate" and text:
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 5.0):"), reply_markup=get_cancel_kb())
                return
                
            country = temp_data[chat_id]["country"]
            service = temp_data[chat_id]["service"]
            raw_numbers = temp_data[chat_id]["numbers"]
            
            clean_nums = []
            for num in raw_numbers:
                num = num.strip()
                if num:
                    if not num.startswith('+'): num = '+' + num
                    clean_nums.append(num)
            
            batch_id = str(uuid.uuid4())[:8]
            number_batches[batch_id] = {
                "filename": temp_data[chat_id]["filename"], 
                "service": service, 
                "country": country, 
                "rate": rate,
                "numbers": [{"num": n, "shares": 0, "used_by": []} for n in clean_nums]
            }
            total_uploaded_stats += len(clean_nums)
            save_db()
            
            app_full_name, prem_app_html = get_service_info_html(service)
            prem_flag_html = get_flag_info_html(clean_nums[0]) if clean_nums else f"{PEM['world']} "
            
            broadcast_txt = f"➖➖➖➖➖➖➖➖\n《 NEW NUMBERS 》\n➖➖➖➖➖➖➖➖\n{prem_flag_html} {country} {prem_app_html} {service}\n➖➖➖➖➖➖➖➖\n📤 Total Added: <b>{len(clean_nums)}</b>\n💰 Reward: <b>{rate} TK</b>\n➖➖➖➖➖➖➖➖\nUse /start to get your numbers!"
            broadcast_txt = render_body_text(broadcast_txt)
            
            send_message(chat_id, render_body_text(f"{PEM['ok']} Numbers added to local stock with {rate} TK rate! Starting broadcast..."))
            
            def simple_broadcast(txt):
                b_session = requests.Session()
                url = f"{BASE_URL}/sendMessage"
                for u_id in list(all_known_users):
                    try:
                        b_session.post(url, json={"chat_id": u_id, "text": txt, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=5)
                    except: pass
                    time.sleep(0.035)
            threading.Thread(target=simple_broadcast, args=(broadcast_txt,)).start()
            
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_stex_key" and text:
            bot_settings["stex_keys"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"✅ StexSMS API Key Added! Total Keys: {len(bot_settings.get('stex_keys', []))}"), reply_markup=stex_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_voltx_key" and text:
            bot_settings["voltx_keys"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"✅ Voltx API Key Added! Total Keys: {len(bot_settings.get('voltx_keys', []))}"), reply_markup=voltx_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_nx_srv_name" and text:
            srv = text.strip().upper()
            if "stex_services" not in bot_settings: bot_settings["stex_services"] = {}
            if srv not in bot_settings["stex_services"]: bot_settings["stex_services"][srv] = {}
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": "manage_stex_srv", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_nx_cnt_name" and text:
            cnt = country_display_name(text)
            srv = temp_data[chat_id]["srv"]
            if cnt not in bot_settings["stex_services"][srv]: bot_settings["stex_services"][srv][cnt] = []
            save_db()
            delete_message(chat_id, msg["message_id"])
            wait_msg_id = temp_data[chat_id]["msg_id"]
            user_states[chat_id] = "wait_nx_cnt_rate"
            temp_data[chat_id] = {"msg_id": wait_msg_id, "srv": srv, "cnt": cnt}
            edit_message(chat_id, wait_msg_id, render_body_text(f"{PEM['money']} Enter OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_nx_cnt_rate" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 0.5):"), reply_markup=get_cancel_kb())
                return
            if "stex_service_rates" not in bot_settings: bot_settings["stex_service_rates"] = {}
            if srv not in bot_settings["stex_service_rates"]: bot_settings["stex_service_rates"][srv] = {}
            bot_settings["stex_service_rates"][srv][cnt] = rate
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"nx_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_nx_addr" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            new_range = text.strip().replace("+", "")
            
            if new_range not in bot_settings["stex_services"][srv][cnt]:
                bot_settings["stex_services"][srv][cnt].append(new_range)
                save_db()
                
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"nx_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_vx_srv_name" and text:
            srv = text.strip().upper()
            if "voltx_services" not in bot_settings: bot_settings["voltx_services"] = {}
            if srv not in bot_settings["voltx_services"]: bot_settings["voltx_services"][srv] = {}
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": "manage_voltx_srv", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_vx_cnt_name" and text:
            cnt = country_display_name(text)
            srv = temp_data[chat_id]["srv"]
            if cnt not in bot_settings["voltx_services"][srv]: bot_settings["voltx_services"][srv][cnt] = []
            save_db()
            delete_message(chat_id, msg["message_id"])
            wait_msg_id = temp_data[chat_id]["msg_id"]
            user_states[chat_id] = "wait_vx_cnt_rate"
            temp_data[chat_id] = {"msg_id": wait_msg_id, "srv": srv, "cnt": cnt}
            edit_message(chat_id, wait_msg_id, render_body_text(f"{PEM['money']} Enter OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_vx_cnt_rate" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 0.5):"), reply_markup=get_cancel_kb())
                return
            if "voltx_service_rates" not in bot_settings: bot_settings["voltx_service_rates"] = {}
            if srv not in bot_settings["voltx_service_rates"]: bot_settings["voltx_service_rates"][srv] = {}
            bot_settings["voltx_service_rates"][srv][cnt] = rate
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"vx_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_vx_addr" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            new_range = text.strip().replace("+", "")
            
            if new_range not in bot_settings["voltx_services"][srv][cnt]:
                bot_settings["voltx_services"][srv][cnt].append(new_range)
                save_db()
                
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"vx_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_for_add_zebrasms_key" and text:
            bot_settings["zebrasms_keys"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"✅ Zebra API Key Added! Total Keys: {len(bot_settings.get('zebrasms_keys', []))}"), reply_markup=zebrasms_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_zb_srv_name" and text:
            srv = text.strip().upper()
            if "zebrasms_services" not in bot_settings: bot_settings["zebrasms_services"] = {}
            if srv not in bot_settings["zebrasms_services"]: bot_settings["zebrasms_services"][srv] = {}
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": "manage_zebrasms_srv", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_zb_cnt_name" and text:
            cnt = country_display_name(text)
            srv = temp_data[chat_id]["srv"]
            if cnt not in bot_settings["zebrasms_services"][srv]: bot_settings["zebrasms_services"][srv][cnt] = []
            save_db()
            delete_message(chat_id, msg["message_id"])
            wait_msg_id = temp_data[chat_id]["msg_id"]
            user_states[chat_id] = "wait_zb_cnt_rate"
            temp_data[chat_id] = {"msg_id": wait_msg_id, "srv": srv, "cnt": cnt}
            edit_message(chat_id, wait_msg_id, render_body_text(f"{PEM['money']} Enter OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_zb_cnt_rate" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 0.5):"), reply_markup=get_cancel_kb())
                return
            if "zebrasms_service_rates" not in bot_settings: bot_settings["zebrasms_service_rates"] = {}
            if srv not in bot_settings["zebrasms_service_rates"]: bot_settings["zebrasms_service_rates"][srv] = {}
            bot_settings["zebrasms_service_rates"][srv][cnt] = rate
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"zb_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_zb_addr" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            new_range = text.strip().replace("+", "")
            if new_range not in bot_settings["zebrasms_services"][srv][cnt]:
                bot_settings["zebrasms_services"][srv][cnt].append(new_range)
                save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"zb_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_for_add_yesms_key" and text:
            bot_settings["yesms_keys"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"✅ Yesms API Key Added! Total Keys: {len(bot_settings.get('yesms_keys', []))}"), reply_markup=yesms_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_ym_srv_name" and text:
            srv = text.strip().upper()
            if "yesms_services" not in bot_settings: bot_settings["yesms_services"] = {}
            if srv not in bot_settings["yesms_services"]: bot_settings["yesms_services"][srv] = {}
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": "manage_yesms_srv", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_ym_cnt_name" and text:
            cnt = country_display_name(text)
            srv = temp_data[chat_id]["srv"]
            if cnt not in bot_settings["yesms_services"][srv]: bot_settings["yesms_services"][srv][cnt] = []
            save_db()
            delete_message(chat_id, msg["message_id"])
            wait_msg_id = temp_data[chat_id]["msg_id"]
            user_states[chat_id] = "wait_ym_cnt_rate"
            temp_data[chat_id] = {"msg_id": wait_msg_id, "srv": srv, "cnt": cnt}
            edit_message(chat_id, wait_msg_id, render_body_text(f"{PEM['money']} Enter OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup=get_cancel_kb())
            return

        elif state == "wait_ym_cnt_rate" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            try:
                rate = float(text.strip())
            except ValueError:
                send_message(chat_id, render_body_text("❌ Invalid rate! Please enter a number (e.g. 0.5):"), reply_markup=get_cancel_kb())
                return
            if "yesms_service_rates" not in bot_settings: bot_settings["yesms_service_rates"] = {}
            if srv not in bot_settings["yesms_service_rates"]: bot_settings["yesms_service_rates"][srv] = {}
            bot_settings["yesms_service_rates"][srv][cnt] = rate
            save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"ym_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_ym_addr" and text:
            srv, cnt = temp_data[chat_id]["srv"], temp_data[chat_id]["cnt"]
            new_range = text.strip().replace("+", "")
            if new_range not in bot_settings["yesms_services"][srv][cnt]:
                bot_settings["yesms_services"][srv][cnt].append(new_range)
                save_db()
            delete_message(chat_id, msg["message_id"])
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": temp_data[chat_id]["msg_id"]}, "data": f"ym_cnt_{srv}_{cnt}", "id": "internal"})
            del user_states[chat_id]
            return

        elif state == "wait_for_add_wm" and text:
            bot_settings["w_methods"].append(text.strip())
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("💳 <b>WITHDRAWAL METHODS</b>\n\nManage your withdrawal methods below:"), reply_markup=w_methods_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_status_srv_name" and text:
            srv = text.strip()
            if "status_services" not in bot_settings: bot_settings["status_services"] = []
            if srv not in bot_settings["status_services"]:
                bot_settings["status_services"].append(srv)
                save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("💠 <b>STATUS Services</b>\nManage the services shown on the STATUS card below:"), reply_markup=status_services_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_fj" and text:
            bot_settings["fj_channels"].append(parse_chat_id(text))
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("🔗 <b>FORCE JOIN SYSTEM</b>\nManage channels below:\n<i>(Note: For private links, use numeric IDs like -100...)</i>"), reply_markup=fj_settings_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return
            
        elif state == "wait_for_add_adm" and text:
            if text.isdigit():
                bot_settings["admins"].append(int(text))
                save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("👥 <b>ADMIN MANAGEMENT</b>\nManage your bot admins below:"), reply_markup=admin_settings_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_add_fw_id" and text:
            bot_settings["fw_groups"].append({"chat_id": text.strip(), "buttons": []})
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return
            
        elif state == "wait_for_add_fw_btn" and text:
            fw_idx = temp_data[chat_id]["fw_idx"]
            if "-" in text:
                parts = text.split("-", 1)
                btn_text = parts[0].strip()
                btn_url = parts[1].strip()
                
                emoji_id = None
                emoji_char = ""
                for ent in msg.get("entities", []):
                    if ent.get("type") == "custom_emoji":
                        emoji_id = ent.get("custom_emoji_id")
                        offset = ent.get("offset", 0)
                        length = ent.get("length", 0)
                        b_text = text.encode('utf-16-le')
                        emoji_char = b_text[offset*2:(offset+length)*2].decode('utf-16-le')
                        break
                
                if emoji_char:
                    btn_text = btn_text.replace(emoji_char, "").strip()
                    
                btn_data = {"text": btn_text, "url": btn_url}
                if emoji_id:
                    btn_data["icon_custom_emoji_id"] = emoji_id
                    
                bot_settings["fw_groups"][fw_idx]["buttons"].append(btn_data)
                save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(f"🛡 <b>Manage Group:</b> {bot_settings['fw_groups'][fw_idx]['chat_id']}"), reply_markup=specific_fw_group_keyboard(fw_idx))
            del user_states[chat_id]
            del temp_data[chat_id]
            return
            
        elif state == "wait_for_otp_link" and text:
            bot_settings["otp_link"] = text.strip()
            save_db()
            delete_message(chat_id, msg["message_id"])
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_panel_name" and text:
            p_name = text.strip()
            t_key = temp_data[chat_id].get("add_type", "api")
            msg_id = temp_data[chat_id]["msg_id"]
            delete_message(chat_id, msg["message_id"])
            
            if t_key == "logc":
                user_states[chat_id] = "wait_for_cpanel_url"
                temp_data[chat_id] = {"msg_id": msg_id, "p_data": {
                    "name": p_name, "type": "Auto Captcha Panel", "status": "ON", "records": 0, "login_status": "⏳ Pending First Login"
                }}
                edit_message(chat_id, msg_id, render_body_text("1️⃣ <b>Login URL</b>\n➡️ Panel এর Login Link দিন:"), reply_markup=get_cancel_kb())
                return
            else:
                bot_settings["panels"].append({
                    "name": p_name, "type": "API Panel", "status": "OFF", "api_url": "", "token": "", "records": 0
                })
                save_db()
                handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_api_panels", "id": "internal"})
                if chat_id in user_states: del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
                return

        elif state == "wait_for_p_api" and text:
            idx = temp_data[chat_id]["p_idx"]
            bot_settings["panels"][idx]["api_url"] = text.strip()
            save_db()
            delete_message(chat_id, msg["message_id"])
            p = bot_settings["panels"][idx]
            ui_text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>API URL:</b> <code>{p.get('api_url', 'None')}</code>\n<b>Token:</b> <code>{p.get('token', 'None')}</code>"
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(ui_text), reply_markup=panel_config_keyboard(idx))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_p_tok" and text:
            idx = temp_data[chat_id]["p_idx"]
            bot_settings["panels"][idx]["token"] = text.strip()
            save_db()
            delete_message(chat_id, msg["message_id"])
            p = bot_settings["panels"][idx]
            ui_text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>API URL:</b> <code>{p.get('api_url', 'None')}</code>\n<b>Token:</b> <code>{p.get('token', 'None')}</code>"
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(ui_text), reply_markup=panel_config_keyboard(idx))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_p_fapi" and text:
            idx = temp_data[chat_id]["p_idx"]
            bot_settings["panels"][idx]["full_api_url"] = text.strip()
            save_db()
            delete_message(chat_id, msg["message_id"])
            p = bot_settings["panels"][idx]
            ui_text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>API URL:</b> <code>{p.get('api_url', 'None')}</code>\n<b>Full API URL:</b> <code>{p.get('full_api_url', 'None')}</code>"
            edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(ui_text), reply_markup=panel_config_keyboard(idx))
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_p_rec" and text:
            if text.isdigit():
                idx = temp_data[chat_id]["p_idx"]
                bot_settings["panels"][idx]["records"] = int(text)
                save_db()
                delete_message(chat_id, msg["message_id"])
                p = bot_settings["panels"][idx]
                
                ui_text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>API URL:</b> <code>{p.get('api_url', 'None')}</code>\n<b>Token:</b> <code>{p.get('token', 'None')}</code>"
                edit_message(chat_id, temp_data[chat_id]["msg_id"], render_body_text(ui_text), reply_markup=panel_config_keyboard(idx))
            else:
                send_message(chat_id, render_body_text("❌ Please enter a valid number! Try again."), reply_markup=get_cancel_kb())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "set_TGZ":
            msg_id = temp_data[chat_id]["msg_id"]
            key = temp_data[chat_id]["key"]
            try:
                if key in ["min_withdraw", "otp_reward", "refer_reward"]: bot_settings[key] = float(text)
                elif key in ["cooldown", "num_req", "num_share"]: bot_settings[key] = int(text)
                else: bot_settings[key] = text
                save_db()
                delete_message(chat_id, msg["message_id"])
                edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())
            except:
                delete_message(chat_id, msg["message_id"])
                edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>\n\n❌ Invalid value!"), reply_markup=TGZ_control_keyboard())
            del user_states[chat_id]
            del temp_data[chat_id]
            return

        elif state == "wait_for_withdraw_amount" and text:
            msg_id_to_edit = temp_data[chat_id].get("msg_id")
            try:
                amount = float(text.strip())
                bal = temp_data[chat_id]["balance"]
                min_w = bot_settings['min_withdraw']
                
                if amount < min_w:
                    if msg_id_to_edit: edit_message(chat_id, msg_id_to_edit, render_body_text(f"❌ Minimum withdrawal is {min_w} ৳!\n💰 Balance: {bal} ৳\n\n📝 Enter again:"), reply_markup=get_cancel_kb())
                    return
                if amount > bal:
                    if msg_id_to_edit: edit_message(chat_id, msg_id_to_edit, render_body_text(f"❌ You don't have enough balance!\n💰 Balance: {bal} ৳\n\n📝 Enter again:"), reply_markup=get_cancel_kb())
                    return
                    
                temp_data[chat_id]["amount"] = amount
                user_states[chat_id] = "wait_for_withdraw_number"
                if msg_id_to_edit:
                    edit_message(chat_id, msg_id_to_edit, render_body_text(f"✅ Amount: {amount} ৳\n\n📱 Now send your <b>{temp_data[chat_id]['method']}</b> account number:"), reply_markup=get_cancel_kb())
            except ValueError:
                if msg_id_to_edit: edit_message(chat_id, msg_id_to_edit, render_body_text("❌ Invalid amount!\n\n📝 Please send a valid number:"), reply_markup=get_cancel_kb())
            return
            
        elif state == "wait_for_2fa_key" and text:
            msg_id_to_edit = temp_data.get(chat_id, {}).get("msg_id")
            delete_message(chat_id, msg.get("message_id")) # ইউজারের মেসেজ ডিলিট

            if not msg_id_to_edit:
                send_message(chat_id, render_body_text("❌ Error: Message not found. Try again."))
                del user_states[chat_id]
                return

            try:
                secret = text.strip().replace(" ", "")
                totp = pyotp.TOTP(secret)
                code = totp.now()
                remaining_time = 30 - (int(time.time()) % 30)
                
                success_txt = (
                    f"━━━━━━━━━━━━━━━\n"
                    f"《 🔐 <b>2FA CODE</b> 》\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🔐 <b>CODE:</b> <code>{code}</code>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🕓 <b>EXPIRES IN:</b> {remaining_time}s\n"
                    f"━━━━━━━━━━━━━━━"
                )
                kb = [[{"text": f"Click to copy {code}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": code}, "style": "success"}],
                      [{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": f"ref_2fa_{secret}", "style": "primary"},
                       {"text": "New Code", "icon_custom_emoji_id": "5352552689983067014", "callback_data": "gen_2fa", "style": "danger"}],
                      [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
                
                edit_message(chat_id, msg_id_to_edit, render_body_text(success_txt), reply_markup={"inline_keyboard": kb})
                del user_states[chat_id]
                if chat_id in temp_data: del temp_data[chat_id]
            except Exception:
                error_txt = "━━━━━━━━━━━━━━━\n《 🔑 <b>ENTER 2FA KEY</b> 》\n━━━━━━━━━━━━━━━\n📝 <b>SEND YOUR 2FA SECRET KEY</b>\n━━━━━━━━━━━━━━━\n❌ <b>Invalid Secret Key! Try again.</b>\n━━━━━━━━━━━━━━━"
                cancel_kb = {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_2fa", "style": "danger"}]]}
                edit_message(chat_id, msg_id_to_edit, render_body_text(error_txt), reply_markup=cancel_kb)
            return

        elif state == "wait_for_withdraw_number":
            msg_id_to_edit = temp_data[chat_id].get("msg_id")
            
            method = temp_data[chat_id]["method"]
            amount = temp_data[chat_id]["amount"]
            number = text
            req_id = f"W_{str(uuid.uuid4())[:6].upper()}"
            
            first_name = msg.get("from", {}).get("first_name", "User")
            last_name = msg.get("from", {}).get("last_name", "")
            full_name = f"{first_name} {last_name}".strip()
            
            update_balance(chat_id, -amount)
            pending_withdrawals[req_id] = {"user_id": chat_id, "amount": amount, "method": method, "number": number, "full_name": full_name}
            
            # Save withdrawal history to the local data file.
            if db:
                try:
                    db.collection('withdrawals').document(req_id).set({
                        "user_id": str(chat_id),
                        "amount": amount,
                        "method": method,
                        "status": "pending",
                        "timestamp": local_ops.SERVER_TIMESTAMP
                    })
                except: pass
                
            if bot_settings["w_group"]:
                admin_msg = f"🎙 <b>NEW WITHDRAWAL REQUEST</b>\n\n👤 <b>USER:</b> <a href='tg://user?id={chat_id}'>{full_name}</a>\n💳 <b>WITHDRAWAL:</b> {amount} TK\n🍏 <b>NUMBER:</b> <code>{number}</code>\n🏦 <b>METHOD:</b> {method}\n\n🧾 <b>REQ ID:</b> {req_id}\n👨‍⚖️ <b>PROCESSED BY ADMIN</b>"
                kb = {"inline_keyboard": [[{"text": "APPROVE", "icon_custom_emoji_id": "5352694861990501856", "callback_data": f"wapp_{req_id}", "style": "success"}, {"text": "REJECT", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"wrej_{req_id}", "style": "danger"}]]}
                send_message(bot_settings["w_group"], render_body_text(admin_msg), reply_markup=kb)
            
            kb = {"inline_keyboard": [[{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]}
            success_text = f"{PEM['ok']} Your withdrawal request has been submitted!\n\n🧾 <b>Req ID:</b> {req_id}\n💰 <b>Amount:</b> {amount} ৳\n🏦 <b>Method:</b> {method}\n📱 <b>Number:</b> <code>{number}</code>"
            
            if msg_id_to_edit:
                edit_message(chat_id, msg_id_to_edit, render_body_text(success_text), reply_markup=kb)
            else:
                send_message(chat_id, render_body_text(success_text), reply_markup=kb)
                
            del user_states[chat_id]
            del temp_data[chat_id]
            return

    # --- Regular Commands ---
    if text.startswith("/start"):
        get_user(chat_id)
        
        # --- PROCESS PENDING REFERRAL ---
        if db:
            try:
                doc = db.collection('users').document(str(chat_id)).get()
                if doc.exists:
                    u_data = doc.to_dict()
                    if u_data.get("referred_by") and not u_data.get("ref_paid"):
                        inviter = u_data["referred_by"]
                        db.collection('users').document(str(chat_id)).update({"ref_paid": True})
                        reward = bot_settings.get("refer_reward", 0.2)
                        update_balance(inviter, reward)
                        db.collection('users').document(str(inviter)).update({"total_refers": local_ops.Increment(1)})
                        if inviter in user_cache:
                            user_cache[inviter]["total_refers"] = user_cache[inviter].get("total_refers", 0) + 1
                        ref_msg = (
                            f"{PEM['gift']} <b>New Referral !</b>\n"
                            f"------------------\n"
                            f"🔥 <b>You Received {reward} TK</b>\n"
                            f"------------------\n"
                            f"{PEM['user']} <b>From User ID:</b> <code>{chat_id}</code>"
                        )
                        send_message(inviter, render_body_text(ref_msg))
            except Exception as e:
                print(f"Local referral reward deferred for {chat_id}: {e}")
                    
        c_msg = bot_settings["custom_messages"].get("start", {})
        txt = render_body_text(c_msg.get("text", f"{PEM['hi']} Welcome!"))
        kb = []
        for b in c_msg.get("buttons", []):
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        
        if kb:
            send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})
            send_message(chat_id, render_body_text(f"{PEM['gear']} Navigation Menu:"), reply_markup=main_menu(chat_id))
        else:
            send_message(chat_id, txt, reply_markup=main_menu(chat_id))

    elif text == "Refer":
        u_data = get_user(chat_id)
        ref_link = f"https://t.me/{BOT_USERNAME}?start={chat_id}"
        c_msg = bot_settings["custom_messages"].get("refer", {})
        
        raw_txt = c_msg.get("text", f"{PEM['gift']} Refer").replace("{ref_link}", ref_link).replace("{total_ref}", str(u_data.get('total_refers', 0))).replace("{ref_reward}", str(bot_settings['refer_reward']))
        txt = render_body_text(raw_txt)
        
        kb = [[{"text": "COPY LINK", "icon_custom_emoji_id": "5192739271886282680", "copy_text": {"text": ref_link}, "style": "success"}]]
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        kb.append([{"text": "CLOSE", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        
        send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})

    elif text == "WITHDRAWAL":
        if not bot_settings["withdraw_on"]:
            send_message(chat_id, render_body_text(f"{PEM['no']} Withdrawals are currently disabled."))
            return
        
        u_data = get_user(chat_id)
        bal = u_data.get('balance', 0.0)
        
        c_msg = bot_settings["custom_messages"].get("withdrawal", {})
        raw_txt = c_msg.get("text", "Withdrawal").replace("{bal}", str(bal)).replace("{total_otp}", str(u_data.get('total_otps', 0))).replace("{total_ref}", str(u_data.get('total_refers', 0))).replace("{min_w}", str(bot_settings['min_withdraw']))
        txt = render_body_text(raw_txt)
        
        kb = []
        for m in bot_settings["w_methods"]:
            kb.append([{"text": m.strip(), "icon_custom_emoji_id": "5190899075968441286", "callback_data": f"sel_wm_{m.strip()}", "style": "primary"}])
        
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        kb.append([{"text": "Cancel", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})

    elif text == "Admin Panel" and is_admin(chat_id):
        send_message(chat_id, get_admin_text(), reply_markup=admin_panel_keyboard())

    elif text == "GET NUMBER":
        local_srvs = set([b["service"] for b in number_batches.values() if b["numbers"]])
        stex_srvs = set(bot_settings.get("stex_services", {}).keys())
        voltx_srvs = set(bot_settings.get("voltx_services", {}).keys())
        zebrasms_srvs = set(bot_settings.get("zebrasms_services", {}).keys())
        yesms_srvs = set(bot_settings.get("yesms_services", {}).keys())
        all_services = local_srvs.union(stex_srvs).union(voltx_srvs).union(zebrasms_srvs).union(yesms_srvs)
        
        if not all_services:
            send_message(chat_id, render_body_text(f"{PEM['no']} No numbers or services available!"))
        else:
            c_msg = bot_settings["custom_messages"].get("get_number", {})
            txt = render_body_text(c_msg.get("text", f"{PEM['pin']} Select Service"))
            
            apps_db = bot_settings.get("premium_apps", {})
            kb = []
            for s in all_services:
                emoji_id = "5352694861990501856" # Default icon
                for app_key, app_data in apps_db.items():
                    if s.upper() == app_key or s.upper() in app_key or app_key in s.upper():
                        if "id" in app_data:
                            emoji_id = app_data["id"]
                            break
                kb.append([{"text": f"{s}", "icon_custom_emoji_id": emoji_id, "callback_data": f"g_s_{s}", "style": "primary"}])
            
            for b in c_msg.get("buttons", []): 
                b_copy = b.copy()
                if "style" not in b_copy: b_copy["style"] = "primary"
                kb.append([b_copy])
            kb.append([{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
            
            send_message(chat_id, txt, reply_markup={"inline_keyboard": kb})

    elif text == "2FA ONLINE" or text == "🔐 2FA ONLINE":
        txt = "━━━━━━━━━━━━━━━\n《 🔐 <b>2FA ONLINE</b> 》\n━━━━━━━━━━━━━━━\n<i>Generate your 2FA security code instantly using your secret key.</i>\n━━━━━━━━━━━━━━━"
        kb = [[{"text": "Generate 2fa code", "icon_custom_emoji_id": "5353022963132174959", "callback_data": "gen_2fa", "style": "success"}],
              [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
        send_message(chat_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif text == "STATUS":
        send_message(chat_id, render_body_text(build_status_report_text()), reply_markup=status_kb())

    elif text == "SUPPORT":
        c_msg = bot_settings["custom_messages"].get("support", {})
        txt = render_body_text(c_msg.get("text", f"{PEM['msg']} Support"))
        if not txt.strip(): txt = render_body_text(f"{PEM['msg']} Support")
        kb = []
        for b in c_msg.get("buttons", []):
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
            
        sup_link = bot_settings.get("support_link", "")
        if sup_link:
            kb.insert(0, [{"text": "Contact Support", "icon_custom_emoji_id": "5337302974806922068", "url": sup_link, "style": "success"}])
            
        kb.append([{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        send_message(chat_id, txt, reply_markup={"inline_keyboard": kb} if kb else None)

    elif text == "Leader Board":
        send_leaderboard(chat_id)

def build_alltime_leaderboard_section():
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    body = ""
    try:
        if not db:
            return "└ <i>Leaderboard unavailable right now.</i>\n"
        top_users = db.collection('users').order_by("total_otps", direction="DESCENDING").limit(10).stream()
        rank = 1
        for u in top_users:
            d = u.to_dict()
            otp_count = d.get("total_otps", 0)
            if otp_count <= 0:
                continue
            name = html.escape(d.get("display_name", "User") or "User")
            rank_icon = medals.get(rank, f"{rank}.")
            body += f"{rank_icon} <b>{name}</b>\n    {PEM['user']} ID: <code>{u.id}</code> │ {PEM['msg']} OTPs: <b>{otp_count}</b>\n\n"
            rank += 1
        if not body:
            body = "└ <i>No OTPs claimed yet. Be the first!</i>\n"
    except Exception:
        body = "└ <i>Failed to load leaderboard. Try again.</i>\n"
    return body

# 🌟 The 7-day board is expensive to recompute (and resets on a 7-day cycle),
# so it's cached and only rebuilt every 8 days - the all-time board above is
# always computed fresh on every 5-second refresh.
_weekly_lb_cache = {"body": "", "built_at": 0}
WEEKLY_LB_REFRESH_SECS = 8 * 24 * 60 * 60   # 8 days
WEEKLY_LB_RESET_SECS = 7 * 24 * 60 * 60     # 7 days

def _reset_weekly_otps_if_due():
    now = time.time()
    last_reset = bot_settings.get("weekly_reset_at", 0)
    if now - last_reset < WEEKLY_LB_RESET_SECS:
        return
    try:
        docs = db.collection('users').where("weekly_otps", ">", 0).stream()
        batch = db.batch()
        n = 0
        for d in docs:
            batch.update(d.reference, {"weekly_otps": 0})
            n += 1
            if n >= 400:
                batch.commit()
                batch = db.batch()
                n = 0
        if n:
            batch.commit()
    except Exception as e:
        print(f"❌ Weekly leaderboard reset error: {e}")
    bot_settings["weekly_reset_at"] = now
    save_db()

def build_weekly_leaderboard_section(force=False):
    now = time.time()
    if not force and (now - _weekly_lb_cache["built_at"] < WEEKLY_LB_REFRESH_SECS) and _weekly_lb_cache["body"]:
        return _weekly_lb_cache["body"]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    body = ""
    try:
        if not db:
            body = "└ <i>Leaderboard unavailable right now.</i>\n"
        else:
            _reset_weekly_otps_if_due()
            top_users = db.collection('users').order_by("weekly_otps", direction="DESCENDING").limit(10).stream()
            rank = 1
            for u in top_users:
                d = u.to_dict()
                otp_count = d.get("weekly_otps", 0)
                if otp_count <= 0:
                    continue
                name = html.escape(d.get("display_name", "User") or "User")
                rank_icon = medals.get(rank, f"{rank}.")
                body += f"{rank_icon} <b>{name}</b>\n    {PEM['user']} ID: <code>{u.id}</code> │ {PEM['msg']} OTPs: <b>{otp_count}</b>\n\n"
                rank += 1
            if not body:
                body = "└ <i>No OTPs claimed this week yet.</i>\n"
    except Exception:
        body = "└ <i>Failed to load leaderboard. Try again.</i>\n"

    _weekly_lb_cache["body"] = body
    _weekly_lb_cache["built_at"] = now
    return body

def build_leaderboard_text(view="both"):
    # 🌟 FIX: added the missing "7-Day" / "All-Time" toggle - the underlying
    # section builders already existed, they just weren't reachable as
    # separate views before.
    updated_at = datetime.now().strftime("%I:%M:%S %p")
    if view == "alltime":
        alltime_body = build_alltime_leaderboard_section()
        return (
            f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>ALL-TIME LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{alltime_body}"
            f"━━━━━━━━━━━━━━━\n<i>{PEM['refresh']} Refreshes every 5 sec • Updated {updated_at}</i>"
        )
    if view == "weekly":
        weekly_body = build_weekly_leaderboard_section()
        return (
            f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>7-DAY LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{weekly_body}"
            f"━━━━━━━━━━━━━━━\n<i>{PEM['refresh']} Refreshes every 8 days • Updated {updated_at}</i>"
        )
    alltime_body = build_alltime_leaderboard_section()
    weekly_body = build_weekly_leaderboard_section()
    return (
        f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>ALL-TIME LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{alltime_body}"
        f"━━━━━━━━━━━━━━━\n《 {PEM['crown']} <b>7-DAY LEADER BOARD</b> 》\n━━━━━━━━━━━━━━━\n{weekly_body}"
        f"━━━━━━━━━━━━━━━\n<i>{PEM['refresh']} All-time refreshes every 5 sec, 7-day every 8 days • Updated {updated_at}</i>"
    )

def leaderboard_kb(view="both"):
    # Only the two requested views are exposed.  There is intentionally no
    # "Both" button; the initial screen may still show both sections.
    row = [
        {"text": "All-Time", "icon_custom_emoji_id": "5352877703043258544", "callback_data": "lb_view_alltime", "style": "primary"},
        {"text": "7-Day", "icon_custom_emoji_id": "5375338737028841420", "callback_data": "lb_view_weekly", "style": "primary"},
    ]
    return {"inline_keyboard": [
        row,
        [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "lb_close", "style": "danger"}]
    ]}

# 🌟 Tracks open Leader Board messages (and which view each is showing) so we
# can auto-refresh them every 5 seconds.
active_leaderboard_messages = {}

def send_leaderboard(chat_id, view="alltime"):
    res = send_message(chat_id, render_body_text(build_leaderboard_text(view)), reply_markup=leaderboard_kb(view))
    if res and res.get("ok"):
        active_leaderboard_messages[chat_id] = {"msg_id": res["result"]["message_id"], "view": view}

def leaderboard_auto_refresh_thread():
    while True:
        time.sleep(5)  # 5 seconds - only the all-time section is truly recomputed this often
        for cid, info in list(active_leaderboard_messages.items()):
            mid, view = info["msg_id"], info.get("view", "both")
            try:
                res = edit_message(cid, mid, render_body_text(build_leaderboard_text(view)), reply_markup=leaderboard_kb(view))
                if not res or not res.get("ok"):
                    active_leaderboard_messages.pop(cid, None)
            except Exception:
                active_leaderboard_messages.pop(cid, None)


def expire_previous_number(chat_id):
    if chat_id in user_active_sessions:
        prev_data = user_active_sessions[chat_id]
        prev_msg_id = prev_data["msg_id"]
        nums = prev_data["nums"]
        
        # API সিস্টেম থেকে রিমুভ করা যাতে ইনবক্সে আর মেসেজ না যায়
        for num in nums:
            if num in stex_assigned_numbers: del stex_assigned_numbers[num]
            if num in voltx_assigned_numbers: del voltx_assigned_numbers[num]
            if num in zebrasms_assigned_numbers: del zebrasms_assigned_numbers[num]
            if num in yesms_assigned_numbers: del yesms_assigned_numbers[num]
        save_db()
        
        # আগের মেসেজ ইডিট করে Expired বাটন বসানো
        kb = [[{"text": "Number Expired", "icon_custom_emoji_id": "5336997731481193790", "callback_data": "ignore", "style": "danger"}]]
        try:
            edit_message(chat_id, prev_msg_id, render_body_text(f"{PEM['no']} <b>Number Expired</b>"), reply_markup={"inline_keyboard": kb})
        except:
            pass
        del user_active_sessions[chat_id]

# ==========================================
# Callback Query Handler
# ==========================================
def handle_callback(call):
    global total_assigned_stats
    chat_id = call["message"]["chat"]["id"]
    chat_type = call["message"]["chat"].get("type", "private")
    data = call.get("data", "")

    # 🌟 Button Loading Fix: বাটন চাপার সাথে সাথেই টেলিগ্রামকে Response দিয়ে দেওয়া, যাতে বাটন আটকে না থাকে!
    if not data.startswith("test_p_conn_") and not data.startswith("c_n_") and not data.startswith("g_c_"):
        try: threading.Thread(target=answer_callback, args=(call["id"],)).start()
        except: pass

    if chat_type != "private" and not (data.startswith("wapp_") or data.startswith("wrej_")):
        return

    msg_id = call["message"]["message_id"]

    if chat_type == "private":
        register_user_local(chat_id)
        sync_user_display_name(chat_id, call)  # 🌟 Keep name fresh even for button-only users

        if is_user_banned(chat_id):
            answer_callback(call["id"], "🚫 You are banned from using this bot!", show_alert=True)
            return

        if not check_force_join(chat_id) and data != "check_fj":
            send_force_join_msg(chat_id)
            return

    if data == "check_fj":
        if check_force_join(chat_id):
            delete_message(chat_id, msg_id)
            send_message(chat_id, render_body_text(f"{PEM['ok']} Thanks for joining! You can now use the bot."), reply_markup=main_menu(chat_id))
            
            # --- PROCESS PENDING REFERRAL ---
            if db:
                doc = db.collection('users').document(str(chat_id)).get()
                if doc.exists:
                    u_data = doc.to_dict()
                    if u_data.get("referred_by") and not u_data.get("ref_paid"):
                        inviter = u_data["referred_by"]
                        db.collection('users').document(str(chat_id)).update({"ref_paid": True})
                        reward = bot_settings.get("refer_reward", 0.2)
                        update_balance(inviter, reward)
                        db.collection('users').document(str(inviter)).update({"total_refers": local_ops.Increment(1)})
                        if inviter in user_cache:
                            user_cache[inviter]["total_refers"] = user_cache[inviter].get("total_refers", 0) + 1
                        ref_msg = (
                            f"{PEM['gift']} <b>New Referral !</b>\n"
                            f"------------------\n"
                            f"🔥 <b>You Received {reward} TK</b>\n"
                            f"------------------\n"
                            f"{PEM['user']} <b>From User ID:</b> <code>{chat_id}</code>"
                        )
                        send_message(inviter, render_body_text(ref_msg))
        else:
            answer_callback(call["id"], "❌ You haven't joined all channels yet!", show_alert=True)
        return

    if data == "close_msg":
        delete_message(chat_id, msg_id)

    elif data == "lb_close":
        active_leaderboard_messages.pop(chat_id, None)
        delete_message(chat_id, msg_id)

    elif data in ("lb_view_alltime", "lb_view_weekly"):
        view = data.replace("lb_view_", "")
        edit_message(chat_id, msg_id, render_body_text(build_leaderboard_text(view)), reply_markup=leaderboard_kb(view))
        if chat_id in active_leaderboard_messages:
            active_leaderboard_messages[chat_id]["view"] = view
        answer_callback(call["id"], f"{PEM['refresh']} Switched!")

    elif data == "status_refresh":
        edit_message(chat_id, msg_id, render_body_text(build_status_report_text()), reply_markup=status_kb())
        answer_callback(call["id"], "🔄 Refreshed!")

    elif data == "status_close":
        delete_message(chat_id, msg_id)

    elif data == "manage_status_srv":
        edit_message(chat_id, msg_id, render_body_text("💠 <b>STATUS Services</b>\nManage the services shown on the STATUS card below:"), reply_markup=status_services_keyboard())

    elif data == "status_add_srv":
        user_states[chat_id] = "wait_status_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter the service name to track (e.g. 1XBET, TELEGRAM):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_status_srv", "style": "danger"}]]})

    elif data.startswith("status_del_srv_"):
        idx = int(data.replace("status_del_srv_", ""))
        services = bot_settings.get("status_services", [])
        if 0 <= idx < len(services):
            services.pop(idx)
            save_db()
        edit_message(chat_id, msg_id, render_body_text("💠 <b>STATUS Services</b>\nManage the services shown on the STATUS card below:"), reply_markup=status_services_keyboard())

    elif data == "cancel_state":
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        delete_message(chat_id, msg_id)

    elif data == "cancel_2fa":
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        txt = "━━━━━━━━━━━━━━━\n《 🔐 <b>2FA ONLINE</b> 》\n━━━━━━━━━━━━━━━\n<i>Generate your 2FA security code instantly using your secret key.</i>\n━━━━━━━━━━━━━━━"
        kb = [[{"text": "Generate 2fa code", "icon_custom_emoji_id": "5353022963132174959", "callback_data": "gen_2fa", "style": "success"}],
              [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})
        answer_callback(call["id"])

    elif data == "gen_2fa":
        user_states[chat_id] = "wait_for_2fa_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        txt = "━━━━━━━━━━━━━━━\n《 🔑 <b>ENTER 2FA KEY</b> 》\n━━━━━━━━━━━━━━━\n📝 <b>SEND YOUR 2FA SECRET KEY</b>\n━━━━━━━━━━━━━━━"
        kb = {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_2fa", "style": "danger"}]]}
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup=kb)
        answer_callback(call["id"])

    elif data.startswith("ref_2fa_"):
        secret = data.replace("ref_2fa_", "")
        try:
            totp = pyotp.TOTP(secret)
            code = totp.now()
            remaining_time = 30 - (int(time.time()) % 30)
            
            success_txt = (
                f"━━━━━━━━━━━━━━━\n"
                f"《 🔐 <b>2FA CODE</b> 》\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🔐 <b>CODE:</b> <code>{code}</code>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"🕓 <b>EXPIRES IN:</b> {remaining_time}s\n"
                f"━━━━━━━━━━━━━━━"
            )
            kb = [[{"text": f"Click to copy {code}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": code}, "style": "success"}],
                  [{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": f"ref_2fa_{secret}", "style": "primary"},
                   {"text": "New Code", "icon_custom_emoji_id": "5352552689983067014", "callback_data": "gen_2fa", "style": "danger"}],
                  [{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}]]
            
            edit_message(chat_id, msg_id, render_body_text(success_txt), reply_markup={"inline_keyboard": kb})
        except:
            answer_callback(call["id"], "❌ Error refreshing code!", show_alert=True)

    elif data == "cancel_TGZ_edit":
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())
        
    elif data == "dummy_alert":
        answer_callback(call["id"], "This feature will be added later!", show_alert=True)
        
    # --- User Management Flows Integration ---
    elif data == "user_management":
        edit_message(chat_id, msg_id, get_user_management_text(), reply_markup=user_management_keyboard())

    elif data == "um_manage_balance":
        user_states[chat_id] = "wait_for_um_bal_uid"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID to Manage Balance:"), reply_markup=get_cancel_kb())
        
    elif data == "um_ban_unban":
        user_states[chat_id] = "wait_for_um_ban_uid"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID to Ban or Unban:"), reply_markup=get_cancel_kb())

    elif data == "um_user_profile":
        user_states[chat_id] = "wait_for_um_prof_uid"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID to View Profile:"), reply_markup=get_cancel_kb())

    # --- Menu Design Integration ---
    elif data == "menu_design_list":
        edit_message(chat_id, msg_id, render_body_text(f"🎨 <b>Menu Design Editor</b>\n\nSelect a menu block to edit its Body Text and Inline Buttons. You can use Premium Emojis too!"), reply_markup=menu_design_list_keyboard())

    elif data == "md_reset_defaults":
        bot_settings["custom_messages"] = DEFAULT_CUSTOM_MESSAGES.copy()
        save_db()
        answer_callback(call["id"], "✅ Resetted to Premium Defaults!", show_alert=True)

    elif data.startswith("md_edit_"):
        answer_callback(call["id"])
        if chat_id in user_states: del user_states[chat_id]
        if chat_id in temp_data: del temp_data[chat_id]
        key = data.replace("md_edit_", "")
        cm_text = render_body_text(bot_settings["custom_messages"].get(key, {}).get("text", "..."))
        try:
            edit_message(chat_id, msg_id, render_body_text(f"🎨 <b>Editing: {key.upper()}</b>\n\nPreview of current Text:\n{cm_text}"), reply_markup=menu_edit_options_keyboard(key))
        except: pass

    elif data.startswith("md_text_"):
        key = data.replace("md_text_", "")
        user_states[chat_id] = "wait_for_menu_text"
        temp_data[chat_id] = {"msg_id": msg_id, "menu_key": key}
        edit_message(chat_id, msg_id, render_body_text(f"📝 <b>Edit Body: {key.upper()}</b>\n\nSend the new text. You can use Premium Emojis directly here.\n(Use standard HTML like <b>bold</b>, <i>italic</i> for formatting)"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"md_edit_{key}", "style": "danger"}]]})

    elif data.startswith("md_btns_"):
        answer_callback(call["id"]) 
        if chat_id in user_states: del user_states[chat_id] 
        if chat_id in temp_data: del temp_data[chat_id]
        key = data.replace("md_btns_", "")
        try:
            edit_message(chat_id, msg_id, render_body_text(f"⚙️ <b>Edit Inline Buttons: {key.upper()}</b>"), reply_markup=menu_buttons_list_keyboard(key))
        except: pass

    elif data.startswith("md_addbtn_"):
        key = data.replace("md_addbtn_", "")
        user_states[chat_id] = "wait_for_menu_btn"
        temp_data[chat_id] = {"msg_id": msg_id, "menu_key": key}
        edit_message(chat_id, msg_id, render_body_text(f"➕ <b>Add Button: {key.upper()}</b>\n\nSend custom button in this format:\n<code>Button Text - https://link.com</code>\n\n<i>(Only normal Emojis supported here!)</i>"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"md_btns_{key}", "style": "danger"}]]})

    elif data.startswith("md_delbtn_"):
        parts = data.split("_")
        key = parts[2]
        b_idx = int(parts[3])
        if b_idx < len(bot_settings["custom_messages"][key]["buttons"]):
            del bot_settings["custom_messages"][key]["buttons"][b_idx]
            save_db()
            answer_callback(call["id"], "✅ Button Deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text(f"⚙️ <b>Edit Inline Buttons: {key.upper()}</b>"), reply_markup=menu_buttons_list_keyboard(key))

    elif data.startswith("sel_wm_"):
        method = data.replace("sel_wm_", "")
        bal = get_user(chat_id).get('balance', 0.0)
        min_w = bot_settings['min_withdraw']
        
        if bal < min_w:
            answer_callback(call["id"], f"❌ আপনার ব্যালেন্স অপর্যাপ্ত! মিনিমাম {min_w} ৳ প্রয়োজন।", show_alert=True)
            return
            
        temp_data[chat_id] = {"method": method, "balance": bal, "msg_id": msg_id}
        user_states[chat_id] = "wait_for_withdraw_amount"
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['ok']} Method: {method}\n💰 Available Balance: {bal} ৳\n\n📝 Enter the amount you want to withdraw (Min: {min_w} ৳):"), reply_markup=get_cancel_kb())
        answer_callback(call["id"])

    elif data == "test_message_flow":
        user_states[chat_id] = "wait_for_test_service"
        temp_data[chat_id] = {}
        edit_message(chat_id, msg_id, render_body_text("🧪 <b>Test Mode</b>\n\n📝 Send the Service Name (e.g., IG):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]]})

    elif data == "manage_emojis":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['star']} <b>Premium Emoji Management</b>\n\nUpload your TXT files or manually add them below:"), reply_markup=emoji_settings_keyboard())

    elif data == "up_flags_txt":
        user_states[chat_id] = "wait_for_flag_txt"
        edit_message(chat_id, msg_id, render_body_text("📂 Please upload the <b>Flag Emojis</b> <code>.txt</code> file."), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_emojis", "style": "danger"}]]})

    elif data == "up_apps_txt":
        user_states[chat_id] = "wait_for_app_txt"
        edit_message(chat_id, msg_id, render_body_text("📂 Please upload the <b>Service Apps</b> <code>.txt</code> file."), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_emojis", "style": "danger"}]]})

    elif data == "add_single_emoji":
        user_states[chat_id] = "wait_for_emoji_extract"
        edit_message(chat_id, msg_id, render_body_text("📝 যেকোনো একটি Premium Emoji সেন্ড করুন (যেমন: 🇧🇩 বা 🚫):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_emojis", "style": "danger"}]]})

    elif data == "dl_flags_txt":
        content = generate_emoji_txt("flags")
        if content:
            send_document(chat_id, "Flag_Emojis.txt", content)
            answer_callback(call["id"], "✅ Downloaded!")
        else:
            answer_callback(call["id"], "❌ No Flag Emojis found!", show_alert=True)

    elif data == "dl_apps_txt":
        content = generate_emoji_txt("apps")
        if content:
            send_document(chat_id, "Service_Apps.txt", content)
            answer_callback(call["id"], "✅ Downloaded!")
        else:
            answer_callback(call["id"], "❌ No App Emojis found!", show_alert=True)

    elif data == "del_all_flags":
        bot_settings["premium_flags"] = {}
        save_db()
        answer_callback(call["id"], "✅ All Premium Flags Deleted Successfully!", show_alert=True)

    elif data == "broadcast_msg":
        user_states[chat_id] = "wait_for_broadcast"
        edit_message(chat_id, msg_id, render_body_text("📢 <b>Broadcast Mode</b>\n\nSend the message you want to broadcast (Text, Photo, Video, File etc)."), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]})

    elif data == "upload_num":
        user_states[chat_id] = "wait_for_txt"
        edit_message(chat_id, msg_id, render_body_text("📂 Please upload the numbers in a <b>.txt</b> file."), reply_markup={"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]})

    elif data == "backup_menu":
        edit_message(chat_id, msg_id, render_body_text(
            f"💾 <b>BACKUP & RESTORE</b>\n\n"
            f"Your complete bot data is stored in <code>{html.escape(DATA_FILE)}</code>.\n"
            "Download it before moving hosts, then upload the same JSON file on the new host."
        ), reply_markup={"inline_keyboard": [
            [{"text": "Download Backup", "icon_custom_emoji_id": "5352597830089347330", "callback_data": "download_backup", "style": "success"}],
            [{"text": "Upload Backup", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "upload_backup", "style": "primary"}],
            [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]})

    elif data == "download_backup":
        try:
            send_document(chat_id, "bot_backup.json", db.export_bytes())
            answer_callback(call["id"], "✅ Backup downloaded!")
        except Exception as exc:
            answer_callback(call["id"], f"❌ Backup failed: {exc}", show_alert=True)

    elif data == "upload_backup":
        user_states[chat_id] = "wait_for_backup_upload"
        edit_message(chat_id, msg_id, render_body_text(
            "📤 Send the <b>bot_backup.json</b> file now.\n"
            "The current local data will be replaced after the file is validated."
        ), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "backup_menu", "style": "danger"}]]})

    elif data == "delete_files":
        kb = []
        for b_id, b_data in number_batches.items():
            kb.append([{"text": f"{b_data['filename']} ({len(b_data['numbers'])})", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"del_b_{b_id}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "primary"}])
        txt = "🗑 Select a file to delete:" if len(kb) > 1 else f"{PEM['no']} No files found."
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_b_"):
        b_id = data.split("del_b_")[1]
        if b_id in number_batches:
            del number_batches[b_id]
            save_db()
            answer_callback(call["id"], "✅ File deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "delete_files", "id": call["id"]})

    elif data == "show_used":
        kb = {"inline_keyboard": [[{"text": "Download TXT", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_used", "style": "primary"}], [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['ok']} <b>Total Used Numbers:</b> {len(used_numbers_list)}"), reply_markup=kb)

    elif data == "show_unused":
        unused_count = sum(len(b["numbers"]) for b in number_batches.values())
        kb = {"inline_keyboard": [[{"text": "Download TXT", "icon_custom_emoji_id": "5257969839313526622", "callback_data": "dl_unused", "style": "primary"}], [{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]]}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['rocket']} <b>Total Unused Numbers:</b> {unused_count}"), reply_markup=kb)

    elif data == "dl_used":
        if not used_numbers_list:
            answer_callback(call["id"], "❌ No used numbers found!", show_alert=True)
            return
        content = "\n".join(used_numbers_list).encode('utf-8')
        send_document(chat_id, "used_numbers.txt", content)
        answer_callback(call["id"])

    elif data == "dl_unused":
        unused_list = [n["num"] for b in number_batches.values() for n in b["numbers"]]
        if not unused_list:
            answer_callback(call["id"], "❌ No unused numbers found!", show_alert=True)
            return
        content = "\n".join(unused_list).encode('utf-8')
        send_document(chat_id, "unused_numbers.txt", content)
        answer_callback(call["id"])

    elif data == "lb_main":
        txt = f"━━━━━━━━━━━━━━━\n《 {PEM['admin']} <b>LEADER BOARD MENU</b> 》\n━━━━━━━━━━━━━━━\n<i>Select a category to view the top performers or history.</i>\n━━━━━━━━━━━━━━━"
        kb = [
            [{"text": "Top Referrers", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "lb_top_refs", "style": "primary"}],
            [{"text": "Top OTP Receivers", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "lb_top_otps", "style": "primary"}],
            [{"text": "Withdrawal History", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "lb_w_history", "style": "success"}],
            [{"text": "Back to Admin", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("lb_"):
        sub = data.replace("lb_", "")
        edit_message(chat_id, msg_id, render_body_text("⌛ <i>Fetching Data...</i>"))
        
        num_map = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣", "0": "0️⃣"}
        def get_p_num(n): return "".join([num_map.get(c, c) for c in str(n)])
        
        try:
            if sub == "top_refs":
                title, field, limit, icon = "TOP 5 REFERRERS", "total_refers", 5, PEM.get('user', '👥')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "top_otps":
                title, field, limit, icon = "TOP 5 OTP RECEIVERS", "total_otps", 5, PEM.get('msg', '📩')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "w_history":
                title, limit, icon = "LAST 10 WITHDRAWALS", 10, PEM.get('money', '💸')
                ws = db.collection('withdrawals').order_by('timestamp', direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for w in ws:
                    d = w.to_dict()
                    s = str(d.get('status','Pending')).lower()
                    stat_icon = PEM.get('ok','✅') if s in ["approved","success"] else PEM.get('no','❌') if s=="rejected" else "⏳"
                    uid = d.get('user_id','User')
                    p = "└" if count == limit else "├"
                    res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={uid}'>{uid}</a> ➔ <b>{d.get('amount',0)}৳</b> {stat_icon}\n"
                    count += 1
                if not res_txt: res_txt = "└ <i>No history found.</i>\n"

            final_msg = f"━━━━━━━━━━━━━━━\n{icon} <b>{title}</b>\n━━━━━━━━━━━━━━━\n{res_txt}━━━━━━━━━━━━━━━"
            kb = [[{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": data, "style": "success"}, {"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]
            edit_message(chat_id, msg_id, render_body_text(final_msg), reply_markup={"inline_keyboard": kb})

        except Exception as e:
            edit_message(chat_id, msg_id, render_body_text(f"❌ Error: {e}"), reply_markup={"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]})

    elif data == "lb_main":
        txt = f"━━━━━━━━━━━━━━━\n《 {PEM['admin']} <b>LEADER BOARD MENU</b> 》\n━━━━━━━━━━━━━━━\n<i>Select a category to view the top performers or history.</i>\n━━━━━━━━━━━━━━━"
        kb = [
            [{"text": "Top Referrers", "icon_custom_emoji_id": "5420145051336485498", "callback_data": "lb_top_refs", "style": "primary"}],
            [{"text": "Top OTP Receivers", "icon_custom_emoji_id": "5353001161878182134", "callback_data": "lb_top_otps", "style": "primary"}],
            [{"text": "Withdrawal History", "icon_custom_emoji_id": "5348469219761626211", "callback_data": "lb_w_history", "style": "success"}],
            [{"text": "Back to Admin", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_admin", "style": "danger"}]
        ]
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("lb_"):
        sub = data.replace("lb_", "")
        edit_message(chat_id, msg_id, render_body_text("⌛ <i>Fetching Data...</i>"))
        
        num_map = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "7": "7️⃣", "8": "8️⃣", "9": "9️⃣", "0": "0️⃣"}
        def get_p_num(n): return "".join([num_map.get(c, c) for c in str(n)])
        
        try:
            if sub == "top_refs":
                title, field, limit, icon = "TOP 5 REFERRERS", "total_refers", 5, PEM.get('user', '👥')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "top_otps":
                title, field, limit, icon = "TOP 5 OTP RECEIVERS", "total_otps", 5, PEM.get('msg', '📩')
                users = db.collection('users').order_by(field, direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for u in users:
                    d = u.to_dict()
                    if d.get(field, 0) > 0:
                        p = "└" if count == limit else "├"
                        res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={u.id}'>{u.id}</a> ➔ <b>{d.get(field,0)}</b>\n"
                        count += 1
                if not res_txt: res_txt = "└ <i>No data found.</i>\n"

            elif sub == "w_history":
                title, limit, icon = "LAST 10 WITHDRAWALS", 10, PEM.get('money', '💸')
                ws = db.collection('withdrawals').order_by('timestamp', direction="DESCENDING").limit(limit).stream()
                res_txt = ""
                count = 1
                for w in ws:
                    d = w.to_dict()
                    s = str(d.get('status','Pending')).lower()
                    stat_icon = PEM.get('ok','✅') if s in ["approved","success"] else PEM.get('no','❌') if s=="rejected" else "⏳"
                    uid = d.get('user_id','User')
                    p = "└" if count == limit else "├"
                    res_txt += f"{p} {get_p_num(count)} <a href='tg://user?id={uid}'>{uid}</a> ➔ <b>{d.get('amount',0)}৳</b> {stat_icon}\n"
                    count += 1
                if not res_txt: res_txt = "└ <i>No history found.</i>\n"

            final_msg = f"━━━━━━━━━━━━━━━\n{icon} <b>{title}</b>\n━━━━━━━━━━━━━━━\n{res_txt}━━━━━━━━━━━━━━━"
            kb = [[{"text": "Refresh", "icon_custom_emoji_id": "5420155432272438703", "callback_data": data, "style": "success"}, {"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]
            edit_message(chat_id, msg_id, render_body_text(final_msg), reply_markup={"inline_keyboard": kb})

        except Exception as e:
            edit_message(chat_id, msg_id, render_body_text(f"❌ Error: {e}"), reply_markup={"inline_keyboard": [[{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "lb_main", "style": "danger"}]]})

    elif data == "back_to_admin":
        if chat_id in user_states: del user_states[chat_id]
        edit_message(chat_id, msg_id, get_admin_text(), reply_markup=admin_panel_keyboard())
        
    elif data == "system_settings":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['gear']} <b>System Settings</b>\nManage advanced bot configurations below:"), reply_markup=system_settings_keyboard())

    elif data == "stex_control":
        edit_message(chat_id, msg_id, render_body_text(f"🌐 <b>StexSMS Control Panel</b>\n\nTotal API Keys: {len(bot_settings.get('stex_keys', []))}\nManage your StexSMS API Keys below:"), reply_markup=stex_control_keyboard())

    elif data == "add_stex_key":
        user_states[chat_id] = "wait_for_add_stex_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new StexSMS API Key (e.g. nxa_...):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "stex_control", "style": "danger"}]]})

    elif data == "view_stex_keys":
        kb = []
        for idx, key in enumerate(bot_settings.get("stex_keys", [])):
            safe_name = key[:10] + "..." if len(key)>10 else key
            kb.append([{"text": f"Delete {safe_name}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_nxa_{idx}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "stex_control", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text("🗑 <b>Select StexSMS Key to Delete:</b>"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_nxa_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings.get("stex_keys", [])):
            del bot_settings["stex_keys"][idx]
            save_db()
            answer_callback(call["id"], "✅ StexSMS Key Deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "view_stex_keys", "id": call["id"]})

    elif data == "manage_stex_srv":
        kb = []
        srvs = bot_settings.get("stex_services", {})
        apps_db = bot_settings.get("premium_apps", {})
        for srv in srvs:
            emoji_id = "5257969839313526622"
            for app_key, app_data in apps_db.items():
                if srv.upper() == app_key or srv.upper() in app_key or app_key in srv.upper():
                    if "id" in app_data:
                        emoji_id = app_data["id"]
                        break
            kb.append([{"text": f"{srv}", "icon_custom_emoji_id": emoji_id, "callback_data": f"nx_srv_{srv}", "style": "primary"}])
        kb.append([{"text": "Add New Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "nx_add_srv", "style": "success"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "stex_control", "style": "danger"}])
        edit_message(chat_id, msg_id, render_body_text("📦 <b>StexSMS Services Manager</b>\nManage your API-based dynamic services below:"), reply_markup={"inline_keyboard": kb})

    elif data == "nx_add_srv":
        user_states[chat_id] = "wait_nx_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Enter Service Name (e.g. TELEGRAM):"), reply_markup=get_cancel_kb())

    elif data.startswith("nx_srv_"):
        srv = data.replace("nx_srv_", "")
        kb = []
        countries = bot_settings["stex_services"].get(srv, {})
        for c in countries:
            _, emoji_id = get_country_flag_info(c)
            emoji_id = emoji_id or "5780471598922337683"
            kb.append([{"text": f"{c} ({len(countries[c])} Ranges)", "icon_custom_emoji_id": emoji_id, "callback_data": f"nx_cnt_{srv}_{c}", "style": "primary"}])
        kb.append([{"text": "Add Country", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"nx_add_cnt_{srv}", "style": "success"}])
        kb.append([{"text": "Delete Service", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"nx_del_srv_{srv}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_stex_srv", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📂 <b>Service: {srv}</b>\nManage countries for this service:"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("nx_add_cnt_"):
        srv = data.replace("nx_add_cnt_", "")
        user_states[chat_id] = "wait_nx_cnt_name"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter Country Name for <b>{srv}</b> (e.g. BD, INDIA):"), reply_markup=get_cancel_kb())

    elif data.startswith("nx_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        ranges = bot_settings["stex_services"][srv].get(cnt, [])
        current_rate = bot_settings.get("stex_service_rates", {}).get(srv, {}).get(cnt, bot_settings.get("otp_reward", 0.0))
        
        kb = []
        row = []
        for r in ranges:
            row.append({"text": f"Delete {r}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"nx_dr_{srv}_{cnt}_{r}", "style": "danger"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row: kb.append(row)
        
        kb.append([{"text": "Add Range", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"nx_addr_{srv}_{cnt}", "style": "success"}])
        kb.append([{"text": f"Set Rate ({current_rate} TK/OTP)", "icon_custom_emoji_id": "5190576863226933563", "callback_data": f"nx_setrate_{srv}_{cnt}", "style": "primary"}])
        kb.append([{"text": "Delete Entire Country", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"nx_del_cnt_{srv}_{cnt}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"nx_srv_{srv}", "style": "primary"}])
        
        txt = f"📍 <b>Service: {srv} | Country: {cnt}</b>\n\n<b>Total Ranges:</b> {len(ranges)}\n💰 <b>OTP Rate:</b> {current_rate} TK/OTP\n<i>Click on a range below to delete it, or add a new one.</i>"
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("nx_setrate_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_nx_cnt_rate"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['money']} Enter new OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"nx_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("nx_addr_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_nx_addr"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"📝 Send the new Range for <b>{cnt}</b> (e.g. 88017):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"nx_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("nx_dr_"):
        parts = data.split("_")
        srv, cnt, rng = parts[2], parts[3], parts[4]
        if rng in bot_settings["stex_services"].get(srv, {}).get(cnt, []):
            bot_settings["stex_services"][srv][cnt].remove(rng)
            save_db()
            answer_callback(call["id"], f"✅ Range {rng} deleted!", show_alert=True)
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"nx_cnt_{srv}_{cnt}", "id": call["id"]})

    elif data.startswith("nx_del_srv_"):
        srv = data.replace("nx_del_srv_", "")
        if srv in bot_settings["stex_services"]: del bot_settings["stex_services"][srv]
        if srv in bot_settings.get("stex_service_rates", {}): del bot_settings["stex_service_rates"][srv]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_stex_srv", "id": call["id"]})

    elif data.startswith("nx_del_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[3], parts[4]
        if cnt in bot_settings["stex_services"].get(srv, {}): del bot_settings["stex_services"][srv][cnt]
        if cnt in bot_settings.get("stex_service_rates", {}).get(srv, {}): del bot_settings["stex_service_rates"][srv][cnt]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"nx_srv_{srv}", "id": call["id"]})

    elif data == "voltx_control":
        edit_message(chat_id, msg_id, render_body_text(f"⚡ <b>Voltx Control Panel</b>\n\nTotal API Keys: {len(bot_settings.get('voltx_keys', []))}\nManage your Voltx API Keys below:"), reply_markup=voltx_control_keyboard())

    elif data == "add_voltx_key":
        user_states[chat_id] = "wait_for_add_voltx_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new Voltx API Key:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "voltx_control", "style": "danger"}]]})

    elif data == "view_voltx_keys":
        kb = []
        for idx, key in enumerate(bot_settings.get("voltx_keys", [])):
            safe_name = key[:10] + "..." if len(key)>10 else key
            kb.append([{"text": f"Delete {safe_name}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_vtx_{idx}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "voltx_control", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text("🗑 <b>Select Voltx Key to Delete:</b>"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_vtx_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings.get("voltx_keys", [])):
            del bot_settings["voltx_keys"][idx]
            save_db()
            answer_callback(call["id"], "✅ Voltx Key Deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "view_voltx_keys", "id": call["id"]})

    elif data == "manage_voltx_srv":
        kb = []
        srvs = bot_settings.get("voltx_services", {})
        apps_db = bot_settings.get("premium_apps", {})
        for srv in srvs:
            emoji_id = "5257969839313526622"
            for app_key, app_data in apps_db.items():
                if srv.upper() == app_key or srv.upper() in app_key or app_key in srv.upper():
                    if "id" in app_data: emoji_id = app_data["id"]; break
            kb.append([{"text": f"{srv}", "icon_custom_emoji_id": emoji_id, "callback_data": f"vx_srv_{srv}", "style": "primary"}])
        kb.append([{"text": "Add New Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "vx_add_srv", "style": "success"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "voltx_control", "style": "danger"}])
        edit_message(chat_id, msg_id, render_body_text("⚡ <b>Voltx Services Manager</b>\nManage your API-based dynamic services below:"), reply_markup={"inline_keyboard": kb})

    elif data == "vx_add_srv":
        user_states[chat_id] = "wait_vx_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Enter Service Name (e.g. TELEGRAM):"), reply_markup=get_cancel_kb())

    elif data.startswith("vx_srv_"):
        srv = data.replace("vx_srv_", "")
        kb = []
        countries = bot_settings["voltx_services"].get(srv, {})
        for c in countries:
            _, emoji_id = get_country_flag_info(c)
            emoji_id = emoji_id or "5780471598922337683"
            kb.append([{"text": f"{c} ({len(countries[c])} Ranges)", "icon_custom_emoji_id": emoji_id, "callback_data": f"vx_cnt_{srv}_{c}", "style": "primary"}])
        kb.append([{"text": "Add Country", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"vx_add_cnt_{srv}", "style": "success"}])
        kb.append([{"text": "Delete Service", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"vx_del_srv_{srv}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_voltx_srv", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📂 <b>Service: {srv}</b>\nManage countries for this service:"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("vx_add_cnt_"):
        srv = data.replace("vx_add_cnt_", "")
        user_states[chat_id] = "wait_vx_cnt_name"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter Country Name for <b>{srv}</b> (e.g. BD, INDIA):"), reply_markup=get_cancel_kb())

    elif data.startswith("vx_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        ranges = bot_settings["voltx_services"][srv].get(cnt, [])
        current_rate = bot_settings.get("voltx_service_rates", {}).get(srv, {}).get(cnt, bot_settings.get("otp_reward", 0.0))
        kb = []
        row = []
        for r in ranges:
            row.append({"text": f"Delete {r}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"vx_dr_{srv}_{cnt}_{r}", "style": "danger"})
            if len(row) == 2:
                kb.append(row)
                row = []
        if row: kb.append(row)
        kb.append([{"text": "Add Range", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"vx_addr_{srv}_{cnt}", "style": "success"}])
        kb.append([{"text": f"Set Rate ({current_rate} TK/OTP)", "icon_custom_emoji_id": "5190576863226933563", "callback_data": f"vx_setrate_{srv}_{cnt}", "style": "primary"}])
        kb.append([{"text": "Delete Entire Country", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"vx_del_cnt_{srv}_{cnt}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"vx_srv_{srv}", "style": "primary"}])
        txt = f"📍 <b>Service: {srv} | Country: {cnt}</b>\n\n<b>Total Ranges:</b> {len(ranges)}\n💰 <b>OTP Rate:</b> {current_rate} TK/OTP\n<i>Click on a range below to delete it, or add a new one.</i>"
        edit_message(chat_id, msg_id, render_body_text(txt), reply_markup={"inline_keyboard": kb})

    elif data.startswith("vx_setrate_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_vx_cnt_rate"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['money']} Enter new OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"vx_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("vx_addr_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_vx_addr"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"📝 Send the new Range for <b>{cnt}</b> (e.g. 26134):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"vx_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("vx_dr_"):
        parts = data.split("_")
        srv, cnt, rng = parts[2], parts[3], parts[4]
        if rng in bot_settings["voltx_services"].get(srv, {}).get(cnt, []):
            bot_settings["voltx_services"][srv][cnt].remove(rng)
            save_db()
            answer_callback(call["id"], f"✅ Range {rng} deleted!", show_alert=True)
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"vx_cnt_{srv}_{cnt}", "id": call["id"]})

    elif data.startswith("vx_del_srv_"):
        srv = data.replace("vx_del_srv_", "")
        if srv in bot_settings["voltx_services"]: del bot_settings["voltx_services"][srv]
        if srv in bot_settings.get("voltx_service_rates", {}): del bot_settings["voltx_service_rates"][srv]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_voltx_srv", "id": call["id"]})

    elif data.startswith("vx_del_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[3], parts[4]
        if cnt in bot_settings["voltx_services"].get(srv, {}): del bot_settings["voltx_services"][srv][cnt]
        if cnt in bot_settings.get("voltx_service_rates", {}).get(srv, {}): del bot_settings["voltx_service_rates"][srv][cnt]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"vx_srv_{srv}", "id": call["id"]})

    # --- ZEBRASMS CONTROL CALLBACKS ---
    elif data == "zebrasms_control":
        edit_message(chat_id, msg_id, render_body_text(f"💠 <b>Zebra Control Panel</b>\n\nTotal API Keys: {len(bot_settings.get('zebrasms_keys', []))}\nManage your Zebra API Keys below:"), reply_markup=zebrasms_control_keyboard())

    elif data == "add_zebrasms_key":
        user_states[chat_id] = "wait_for_add_zebrasms_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new Zebra API Key (MAuth):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "zebrasms_control", "style": "danger"}]]})

    elif data == "view_zebrasms_keys":
        kb = []
        for idx, key in enumerate(bot_settings.get("zebrasms_keys", [])):
            safe_name = key[:10] + "..." if len(key)>10 else key
            kb.append([{"text": f"Delete {safe_name}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_zbs_{idx}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "zebrasms_control", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text("🗑 <b>Select Zebra Key to Delete:</b>"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_zbs_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings.get("zebrasms_keys", [])):
            del bot_settings["zebrasms_keys"][idx]
            save_db()
            answer_callback(call["id"], "✅ Zebra Key Deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "view_zebrasms_keys", "id": call["id"]})

    elif data == "manage_zebrasms_srv":
        kb = []
        for srv in bot_settings.get("zebrasms_services", {}):
            kb.append([{"text": f"{srv}", "icon_custom_emoji_id": "5257969839313526622", "callback_data": f"zb_srv_{srv}", "style": "primary"}])
        kb.append([{"text": "Add New Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "zb_add_srv", "style": "success"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "zebrasms_control", "style": "danger"}])
        edit_message(chat_id, msg_id, render_body_text("💠 <b>Zebra Services Manager</b>\nManage your API-based manual services below:"), reply_markup={"inline_keyboard": kb})

    elif data == "zb_add_srv":
        user_states[chat_id] = "wait_zb_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Enter Service Name (e.g. TELEGRAM):"), reply_markup=get_cancel_kb())

    elif data.startswith("zb_srv_"):
        srv = data.replace("zb_srv_", "")
        kb = []
        for c in bot_settings.get("zebrasms_services", {}).get(srv, {}):
            _, emoji_id = get_country_flag_info(c)
            emoji_id = emoji_id or "5780471598922337683"
            kb.append([{"text": f"{c} ({len(bot_settings['zebrasms_services'][srv][c])} Ranges)", "icon_custom_emoji_id": emoji_id, "callback_data": f"zb_cnt_{srv}_{c}", "style": "primary"}])
        kb.append([{"text": "Add Country", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"zb_add_cnt_{srv}", "style": "success"}])
        kb.append([{"text": "Delete Service", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"zb_del_srv_{srv}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_zebrasms_srv", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📂 <b>Service: {srv}</b>\nManage countries for this service:"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("zb_add_cnt_"):
        srv = data.replace("zb_add_cnt_", "")
        user_states[chat_id] = "wait_zb_cnt_name"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter Country Name for <b>{srv}</b> (e.g. UK, USA):"), reply_markup=get_cancel_kb())

    elif data.startswith("zb_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        ranges = bot_settings["zebrasms_services"][srv].get(cnt, [])
        current_rate = bot_settings.get("zebrasms_service_rates", {}).get(srv, {}).get(cnt, bot_settings.get("otp_reward", 0.0))
        kb, row = [], []
        for r in ranges:
            row.append({"text": f"Delete {r}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"zb_dr_{srv}_{cnt}_{r}", "style": "danger"})
            if len(row) == 2: kb.append(row); row = []
        if row: kb.append(row)
        kb.append([{"text": "Add Range", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"zb_addr_{srv}_{cnt}", "style": "success"}])
        kb.append([{"text": f"Set Rate ({current_rate} TK/OTP)", "icon_custom_emoji_id": "5190576863226933563", "callback_data": f"zb_setrate_{srv}_{cnt}", "style": "primary"}])
        kb.append([{"text": "Delete Entire Country", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"zb_del_cnt_{srv}_{cnt}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"zb_srv_{srv}", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📍 <b>Service: {srv} | Country: {cnt}</b>\n\n<b>Total Ranges:</b> {len(ranges)}\n💰 <b>OTP Rate:</b> {current_rate} TK/OTP"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("zb_setrate_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_zb_cnt_rate"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['money']} Enter new OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"zb_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("zb_addr_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_zb_addr"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"📝 Send the new Range for <b>{cnt}</b> (e.g. 22501XXX):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"zb_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("zb_dr_"):
        parts = data.split("_")
        srv, cnt, rng = parts[2], parts[3], parts[4]
        if rng in bot_settings["zebrasms_services"].get(srv, {}).get(cnt, []):
            bot_settings["zebrasms_services"][srv][cnt].remove(rng)
            save_db()
            answer_callback(call["id"], f"✅ Range {rng} deleted!", show_alert=True)
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"zb_cnt_{srv}_{cnt}", "id": call["id"]})

    elif data.startswith("zb_del_srv_"):
        srv = data.replace("zb_del_srv_", "")
        if srv in bot_settings["zebrasms_services"]: del bot_settings["zebrasms_services"][srv]
        if srv in bot_settings.get("zebrasms_service_rates", {}): del bot_settings["zebrasms_service_rates"][srv]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_zebrasms_srv", "id": call["id"]})

    elif data.startswith("zb_del_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[3], parts[4]
        if cnt in bot_settings["zebrasms_services"].get(srv, {}): del bot_settings["zebrasms_services"][srv][cnt]
        if cnt in bot_settings.get("zebrasms_service_rates", {}).get(srv, {}): del bot_settings["zebrasms_service_rates"][srv][cnt]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"zb_srv_{srv}", "id": call["id"]})

    # --- YESMS CONTROL CALLBACKS ---
    elif data == "yesms_control":
        edit_message(chat_id, msg_id, render_body_text(f"📮 <b>Yesms Control Panel</b>\n\nTotal API Keys: {len(bot_settings.get('yesms_keys', []))}\nManage your Yesms API Keys below:"), reply_markup=yesms_control_keyboard())

    elif data == "add_yesms_key":
        user_states[chat_id] = "wait_for_add_yesms_key"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new Yesms API Key:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "yesms_control", "style": "danger"}]]})

    elif data == "view_yesms_keys":
        kb = []
        for idx, key in enumerate(bot_settings.get("yesms_keys", [])):
            safe_name = key[:10] + "..." if len(key)>10 else key
            kb.append([{"text": f"Delete {safe_name}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"del_ysm_{idx}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "yesms_control", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text("🗑 <b>Select Yesms Key to Delete:</b>"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("del_ysm_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings.get("yesms_keys", [])):
            del bot_settings["yesms_keys"][idx]
            save_db()
            answer_callback(call["id"], "✅ Yesms Key Deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "view_yesms_keys", "id": call["id"]})

    elif data == "manage_yesms_srv":
        kb = []
        for srv in bot_settings.get("yesms_services", {}):
            kb.append([{"text": f"{srv}", "icon_custom_emoji_id": "5257969839313526622", "callback_data": f"ym_srv_{srv}", "style": "primary"}])
        kb.append([{"text": "Add New Service", "icon_custom_emoji_id": "5420323438508155202", "callback_data": "ym_add_srv", "style": "success"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "yesms_control", "style": "danger"}])
        edit_message(chat_id, msg_id, render_body_text("📮 <b>Yesms Services Manager</b>\nManage your API-based manual services below:"), reply_markup={"inline_keyboard": kb})

    elif data == "ym_add_srv":
        user_states[chat_id] = "wait_ym_srv_name"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Enter Service Name (e.g. TELEGRAM):"), reply_markup=get_cancel_kb())

    elif data.startswith("ym_srv_"):
        srv = data.replace("ym_srv_", "")
        kb = []
        for c in bot_settings.get("yesms_services", {}).get(srv, {}):
            flag_char, flag_id = get_country_flag_info(c)
            flag_id = flag_id or "5780471598922337683"
            kb.append([{"text": f"{c} ({len(bot_settings['yesms_services'][srv][c])} Ranges)", "icon_custom_emoji_id": flag_id, "callback_data": f"ym_cnt_{srv}_{c}", "style": "primary"}])
        kb.append([{"text": "Add Country", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"ym_add_cnt_{srv}", "style": "success"}])
        kb.append([{"text": "Delete Service", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"ym_del_srv_{srv}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_yesms_srv", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📂 <b>Service: {srv}</b>\nManage countries for this service:"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("ym_add_cnt_"):
        srv = data.replace("ym_add_cnt_", "")
        user_states[chat_id] = "wait_ym_cnt_name"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['world']} Enter Country Name for <b>{srv}</b> (e.g. BD, USA):"), reply_markup=get_cancel_kb())

    elif data.startswith("ym_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        ranges = bot_settings["yesms_services"][srv].get(cnt, [])
        current_rate = bot_settings.get("yesms_service_rates", {}).get(srv, {}).get(cnt, bot_settings.get("otp_reward", 0.0))
        kb, row = [], []
        for r in ranges:
            row.append({"text": f"Delete {r}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"ym_dr_{srv}_{cnt}_{r}", "style": "danger"})
            if len(row) == 2: kb.append(row); row = []
        if row: kb.append(row)
        kb.append([{"text": "Add Range", "icon_custom_emoji_id": "5420323438508155202", "callback_data": f"ym_addr_{srv}_{cnt}", "style": "success"}])
        kb.append([{"text": f"Set Rate ({current_rate} TK/OTP)", "icon_custom_emoji_id": "5190576863226933563", "callback_data": f"ym_setrate_{srv}_{cnt}", "style": "primary"}])
        kb.append([{"text": "Delete Entire Country", "icon_custom_emoji_id": "5422557736330106570", "callback_data": f"ym_del_cnt_{srv}_{cnt}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"ym_srv_{srv}", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"📍 <b>Service: {srv} | Country: {cnt}</b>\n\n<b>Total Ranges:</b> {len(ranges)}\n💰 <b>OTP Rate:</b> {current_rate} TK/OTP"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("ym_setrate_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_ym_cnt_rate"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['money']} Enter new OTP Rate for <b>{cnt}</b> (e.g. 0.5):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"ym_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("ym_addr_"):
        parts = data.split("_")
        srv, cnt = parts[2], parts[3]
        user_states[chat_id] = "wait_ym_addr"
        temp_data[chat_id] = {"msg_id": msg_id, "srv": srv, "cnt": cnt}
        edit_message(chat_id, msg_id, render_body_text(f"📝 Send the new Range ID for <b>{cnt}</b> (e.g. 880X01):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"ym_cnt_{srv}_{cnt}", "style": "danger"}]]})

    elif data.startswith("ym_dr_"):
        parts = data.split("_")
        srv, cnt, rng = parts[2], parts[3], parts[4]
        if rng in bot_settings["yesms_services"].get(srv, {}).get(cnt, []):
            bot_settings["yesms_services"][srv][cnt].remove(rng)
            save_db()
            answer_callback(call["id"], f"✅ Range {rng} deleted!", show_alert=True)
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"ym_cnt_{srv}_{cnt}", "id": call["id"]})

    elif data.startswith("ym_del_srv_"):
        srv = data.replace("ym_del_srv_", "")
        if srv in bot_settings["yesms_services"]: del bot_settings["yesms_services"][srv]
        if srv in bot_settings.get("yesms_service_rates", {}): del bot_settings["yesms_service_rates"][srv]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "manage_yesms_srv", "id": call["id"]})

    elif data.startswith("ym_del_cnt_"):
        parts = data.split("_")
        srv, cnt = parts[3], parts[4]
        if cnt in bot_settings["yesms_services"].get(srv, {}): del bot_settings["yesms_services"][srv][cnt]
        if cnt in bot_settings.get("yesms_service_rates", {}).get(srv, {}): del bot_settings["yesms_service_rates"][srv][cnt]
        save_db()
        handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"ym_srv_{srv}", "id": call["id"]})

    elif data == "manage_fj":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['link']} <b>FORCE JOIN SYSTEM</b>\nManage channels below:"), reply_markup=fj_settings_keyboard())

    elif data == "toggle_fj":
        bot_settings["fj_on"] = not bot_settings["fj_on"]
        save_db()
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['link']} <b>FORCE JOIN SYSTEM</b>\nManage channels below:"), reply_markup=fj_settings_keyboard())

    elif data == "add_fj":
        user_states[chat_id] = "wait_for_add_fj"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send Channel Username or Invite Link:\n<i>(Note: For private channels, use the numeric ID like -100...)</i>"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_fj", "style": "danger"}]]})

    elif data.startswith("del_fj_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["fj_channels"]):
            del bot_settings["fj_channels"][idx]
            save_db()
            answer_callback(call["id"], "✅ Channel deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text(f"{PEM['link']} <b>FORCE JOIN SYSTEM</b>\nManage channels below:"), reply_markup=fj_settings_keyboard())

    elif data == "manage_admins":
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['user']} <b>ADMIN MANAGEMENT</b>\nManage your bot admins below:"), reply_markup=admin_settings_keyboard())

    elif data == "add_adm":
        user_states[chat_id] = "wait_for_add_adm"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the User ID of the new Admin:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_admins", "style": "danger"}]]})

    elif data.startswith("del_adm_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["admins"]):
            del bot_settings["admins"][idx]
            save_db()
            answer_callback(call["id"], "✅ Admin deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text(f"{PEM['user']} <b>ADMIN MANAGEMENT</b>\nManage your bot admins below:"), reply_markup=admin_settings_keyboard())

    elif data == "manage_otp_groups":
        edit_message(chat_id, msg_id, render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())

    elif data == "add_fw":
        user_states[chat_id] = "wait_for_add_fw_id"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the Group ID/Username to forward messages to:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_otp_groups", "style": "danger"}]]})

    elif data.startswith("manage_fw_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["fw_groups"]):
            grp_id = bot_settings["fw_groups"][idx]["chat_id"]
            edit_message(chat_id, msg_id, render_body_text(f"🛡 <b>Manage Group:</b> {grp_id}"), reply_markup=specific_fw_group_keyboard(idx))

    elif data.startswith("add_fwbtn_"):
        idx = int(data.split("_")[2])
        user_states[chat_id] = "wait_for_add_fw_btn"
        temp_data[chat_id] = {"msg_id": msg_id, "fw_idx": idx}
        edit_message(chat_id, msg_id, render_body_text("📝 Send Custom Inline Button format:\n<code>Button Text - https://link.com</code>"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"manage_fw_{idx}", "style": "danger"}]]})

    elif data.startswith("del_fwbtn_"):
        parts = data.split("_")
        idx, b_idx = int(parts[2]), int(parts[3])
        if 0 <= idx < len(bot_settings["fw_groups"]):
            if 0 <= b_idx < len(bot_settings["fw_groups"][idx]["buttons"]):
                del bot_settings["fw_groups"][idx]["buttons"][b_idx]
                save_db()
                answer_callback(call["id"], "✅ Button deleted!", show_alert=True)
                edit_message(chat_id, msg_id, render_body_text(f"🛡 <b>Manage Group:</b> {bot_settings['fw_groups'][idx]['chat_id']}"), reply_markup=specific_fw_group_keyboard(idx))

    elif data.startswith("del_fw_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["fw_groups"]):
            del bot_settings["fw_groups"][idx]
            save_db()
            answer_callback(call["id"], "✅ Group deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text("🛡 <b>OTP GROUP MANAGEMENT</b>\nManage settings below:"), reply_markup=otp_groups_list_keyboard())

    elif data == "edit_otp_link":
        user_states[chat_id] = "wait_for_otp_link"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the new OTP Group Link:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_otp_groups", "style": "danger"}]]})

    elif data == "manage_panels":
        api_count = len([p for p in bot_settings["panels"] if p.get("type") == "API Panel"])
        cpt_count = len([p for p in bot_settings["panels"] if p.get("type", "API Panel") == "Auto Captcha Panel"])
        text = f"{PEM['gear']} <b>Panel Management</b>\n\nSelect which type of panel system you want to manage:"
        kb = {"inline_keyboard": [
            [{"text": f"Manage API Panels ({api_count})", "icon_custom_emoji_id": "5336972142066047577", "callback_data": "manage_api_panels", "style": "primary"}],
            [{"text": f"Manage Auto Captcha Panels ({cpt_count})", "icon_custom_emoji_id": "5353022963132174959", "callback_data": "manage_cpt_panels", "style": "success"}],
            [{"text": "Back to System", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "system_settings", "style": "danger"}]
        ]}
        edit_message(chat_id, msg_id, render_body_text(text), reply_markup=kb)

    elif data in ["manage_api_panels", "manage_cpt_panels"]:
        p_type = "API Panel" if data == "manage_api_panels" else "Auto Captcha Panel"
        p_list = [p for p in bot_settings["panels"] if p.get("type", "API Panel") == p_type]
        icon = f"{PEM['world']} API" if p_type == 'API Panel' else f"{PEM['lock']} Auto Captcha"
        
        text = f"{icon} <b>{p_type}s Management</b>\n\n👀 <b>Active Monitors:</b> {len(p_list)}\n\n🟢 <b>Available Providers:</b>\n"
        for p in p_list:
            status = "Monitoring" if p['status'] == 'ON' else "Stopped"
            login_state = p.get('login_status', '')
            if p['type'] == 'Auto Captcha Panel':
                conf = f" {login_state}" if login_state else f"{PEM['ok']} Configured"
            else:
                conf = f"{PEM['ok']} Configured" if p.get('api_url') else f"{PEM['no']} Not Configured"
            text += f"• {p['name']}: {PEM['ok'] if p['status']=='ON' else PEM['no']} {status} | {conf}\n"
        edit_message(chat_id, msg_id, render_body_text(text), reply_markup=typed_panels_list_keyboard(p_type))

    elif data in ["add_api_panel", "add_cpt_panel"]:
        user_states[chat_id] = "wait_for_panel_name"
        p_type = "api" if data == "add_api_panel" else "logc"
        temp_data[chat_id] = {"msg_id": msg_id, "add_type": p_type}
        edit_message(chat_id, msg_id, render_body_text("📝 Please send the name of the New Provider:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"manage_{'api' if p_type=='api' else 'cpt'}_panels", "style": "danger"}]]})

    elif data.startswith("add_ptype_"):
        pass

    elif data in ["list_del_api", "list_del_cpt"]:
        p_type = "API Panel" if data == "list_del_api" else "Auto Captcha Panel"
        kb = []
        for idx, p in enumerate(bot_settings["panels"]):
            if p.get("type", "API Panel") == p_type:
                kb.append([{"text": f"Delete {p['name']}", "icon_custom_emoji_id": "5420130255174145507", "callback_data": f"do_del_pnl_{idx}", "style": "danger"}])
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"manage_{'api' if p_type=='API Panel' else 'cpt'}_panels", "style": "primary"}])
        edit_message(chat_id, msg_id, render_body_text(f"{PEM['trash']} <b>Select a Provider to Delete:</b>"), reply_markup={"inline_keyboard": kb})

    elif data.startswith("do_del_pnl_"):
        idx = int(data.split("_")[3])
        if 0 <= idx < len(bot_settings["panels"]):
            p_type = bot_settings["panels"][idx].get("type", "API Panel")
            del bot_settings["panels"][idx]
            save_db()
            answer_callback(call["id"], "✅ Provider Deleted!", show_alert=True)
            handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": f"manage_{'api' if p_type=='API Panel' else 'cpt'}_panels", "id": "internal"})

    elif data.startswith("tog_pnl_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["panels"]):
            p = bot_settings["panels"][idx]
            
            p["status"] = "ON" if p["status"] == "OFF" else "OFF"
            save_db()
            
            if p["type"] == "Auto Captcha Panel":
                text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>Login Status:</b> {p.get('login_status', 'Unknown')}\n<b>Login URL:</b> <code>{p.get('login_url', 'None')}</code>\n<b>User:</b> <code>{p.get('username', 'None')}</code>"
            else:
                text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>API URL:</b> <code>{p.get('api_url', 'None')}</code>\n<b>Token:</b> <code>{p.get('token', 'None')}</code>"
            edit_message(chat_id, msg_id, render_body_text(text), reply_markup=panel_config_keyboard(idx))

    elif data.startswith("conf_pnl_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["panels"]):
            p = bot_settings["panels"][idx]
            if p["type"] == "Auto Captcha Panel":
                text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>Login Status:</b> {p.get('login_status', 'Unknown')}\n<b>Login URL:</b> <code>{p.get('login_url', 'None')}</code>\n<b>User:</b> <code>{p.get('username', 'None')}</code>\n<b>Num Col:</b> {p.get('num_col_name')} (Idx: {p.get('num_col_idx')})\n<b>Msg Col:</b> {p.get('msg_col_name')} (Idx: {p.get('msg_col_idx')})"
            else:
                text = f"⚙️ <b>Configure {p['name']}</b>\n\n<b>Type:</b> {p['type']}\n<b>Status:</b> {'🟢 Monitoring' if p['status'] == 'ON' else '🔴 Stopped'}\n<b>API URL:</b> <code>{p.get('api_url', 'None')}</code>\n<b>Token:</b> <code>{p.get('token', 'None')}</code>\n<b>Full API URL:</b> <code>{p.get('full_api_url', 'None')}</code>"
            edit_message(chat_id, msg_id, render_body_text(text), reply_markup=panel_config_keyboard(idx))

    elif data.startswith("set_p_api_"):
        idx = int(data.split("_")[3])
        user_states[chat_id] = "wait_for_p_api"
        temp_data[chat_id] = {"msg_id": msg_id, "p_idx": idx}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the API URL for this provider:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"conf_pnl_{idx}", "style": "danger"}]]})

    elif data.startswith("set_p_tok_"):
        idx = int(data.split("_")[3])
        user_states[chat_id] = "wait_for_p_tok"
        temp_data[chat_id] = {"msg_id": msg_id, "p_idx": idx}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the Token for this provider:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"conf_pnl_{idx}", "style": "danger"}]]})

    elif data.startswith("set_p_fapi_"):
        idx = int(data.split("_")[3])
        user_states[chat_id] = "wait_for_p_fapi"
        temp_data[chat_id] = {"msg_id": msg_id, "p_idx": idx}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the FULL API URL (Example: http://api.com/get?key=YOUR_TOKEN&start=0):"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"conf_pnl_{idx}", "style": "danger"}]]})

    elif data.startswith("set_p_rec_"):
        idx = int(data.split("_")[3])
        user_states[chat_id] = "wait_for_p_rec"
        temp_data[chat_id] = {"msg_id": msg_id, "p_idx": idx}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the number of records to fetch (e.g. 10).\nType <code>0</code> for Unlimited:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": f"conf_pnl_{idx}", "style": "danger"}]]})

    elif data.startswith("test_p_conn_"):
        idx = int(data.split("_")[3])
        p = bot_settings["panels"][idx]
        wait_msg = send_message(chat_id, render_body_text("⏳ Testing connection. Please wait..."))
        wait_msg_id = wait_msg.get("result", {}).get("message_id") if wait_msg else None
        answer_callback(call["id"])
        
        try:
            parsed = []
            raw_text = ""
            
            if p["type"] == "Auto Captcha Panel":
                sess = panel_sessions.get(idx)
                if not sess:
                    success = attempt_auto_login(p, idx)
                    if not success:
                        if wait_msg_id: delete_message(chat_id, wait_msg_id)
                        send_message(chat_id, render_body_text(f"❌ <b>Auto Login Failed!</b>\nReason: {html.escape(str(p.get('login_status', 'Unknown')))}"))
                        return
                    sess = panel_sessions.get(idx)
                    
                login_url = p.get("login_url", "").strip()
                if not login_url.startswith("http"): login_url = "http://" + login_url
                msg_link = p.get("msg_link", "").strip()
                if not msg_link.startswith("http") and msg_link != "": msg_link = "http://" + msg_link
                check_url = msg_link if msg_link else f"{login_url.split('/login')[0]}/client/SMSCDRStats"
                
                # 🌟 test connection supports sAjaxSource & HTML table parser
                parsed, raw_text = fetch_cpt_panel_cdrs(p, sess, check_url)
                
            else:
                full_url = p.get("full_api_url", "").strip()
                url = p.get("api_url", "").strip()
                token = p.get("token", "").strip()
                if not full_url and not url:
                    if wait_msg_id: delete_message(chat_id, wait_msg_id)
                    send_message(chat_id, render_body_text("❌ Please Set API URL or Full API URL first!"))
                    return
                
                urls_to_try = []
                if full_url:
                    urls_to_try.append(full_url)
                else:
                    if "{token}" in url or "{key}" in url:
                        urls_to_try.append(url.replace("{token}", token).replace("{key}", token))
                    elif "token=" in url or "key=" in url:
                        urls_to_try.append(url)
                    else:
                        sep = '&' if '?' in url else '?'
                        urls_to_try.append(f"{url}{sep}token={token}")
                        urls_to_try.append(f"{url}{sep}key={token}&start=0")
                        urls_to_try.append(f"{url}{sep}key={token}")
                    
                parsed = []
                raw_text = ""
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                for try_url in urls_to_try:
                    try:
                        res = requests.get(try_url, headers=headers, timeout=10)
                        raw_text = res.text
                        parsed = parse_panel_response(raw_text, p)
                        if parsed:
                            if not full_url and try_url != url and token:
                                p["api_url"] = try_url.replace(token, "{token}")
                                save_db()
                            break
                    except: pass
                 
            if wait_msg_id: delete_message(chat_id, wait_msg_id)
                 
            if parsed:
                txt = f"✅ <b>Connection Successful!</b>\n\n🎯 <b>Parsed Data Sample (Max 3):</b>\n\n"
                
                for i, sample in enumerate(parsed[:3]):
                    num = sample['number']
                    msg = sample['message']
                    otp = sample['otp']
                    
                    detected_app = detect_service(msg)
                    app_name = detected_app if detected_app else p.get("name", "Unknown")
                    app_full_name, prem_app_html = get_service_info_html(app_name, msg)
                    
                    txt += f"<b>{i+1}.</b> {prem_app_html} <b>{app_full_name}</b>\n"
                    txt += f"📱 Number: <code>{num}</code>\n"
                    txt += f"📝 Full Msg: <code>{html.escape(msg)}</code>\n"
                    txt += f"🔐 OTP: <code>{otp}</code>\n"
                    txt += "➖" * 12 + "\n"
                    
                send_message(chat_id, render_body_text(txt))
            else:
                if p["type"] == "Auto Captcha Panel":
                    try:
                        soup = BeautifulSoup(raw_text, 'html.parser')
                        tables = soup.find_all('table')
                        if tables:
                            full_table_data = "🔍 FULL TABLE DATA (A-Z)\n" + "="*50 + "\n\n"
                            for t_idx, table in enumerate(tables):
                                full_table_data += f"--- Table {t_idx+1} ---\n"
                                rows = table.find_all('tr')
                                for r_idx, row in enumerate(rows):
                                    cols = row.find_all(['th', 'td'])
                                    col_texts = [f"[{c_idx+1}] {c.get_text(separator=' ', strip=True)}" for c_idx, c in enumerate(cols)]
                                    full_table_data += f"Row {r_idx+1}: {' | '.join(col_texts)}\n"
                                full_table_data += "\n" + "="*50 + "\n"
                            
                            send_document(chat_id, f"Full_Panel_Data_{idx}.txt", full_table_data.encode('utf-8'))
                            fail_txt = f"⚠️ <b>Connected, but couldn't parse OTP data!</b>\n\n<i>আমি ওই লিংকের সম্পূর্ণ (A-Z) ডাটা একটি Text File এ পাঠিয়েছি। ফাইলটি ওপেন করে সঠিক Column Number (যেমন: [1], [3]) চেক করে প্যানেলে আপডেট করে নাও।</i>"
                            send_message(chat_id, render_body_text(fail_txt))
                        else:
                            send_message(chat_id, render_body_text(f"⚠️ <b>Connected, but no HTML Table found!</b>\nMake sure the message link is correct."))
                    except Exception as e:
                        send_message(chat_id, render_body_text(f"❌ <b>Error parsing HTML:</b> {html.escape(str(e))}"))
                else:
                    safe_html = html.escape(str(raw_text)[:300])
                    send_message(chat_id, render_body_text(f"⚠️ <b>Connected, but couldn't find/parse OTP data.</b>\n\n<i>Make sure your API config is correct.</i>\n\nRaw HTML/Data (excerpt):\n<code>{safe_html}...</code>"))
        except Exception as e:
            if wait_msg_id: delete_message(chat_id, wait_msg_id)
            send_message(chat_id, render_body_text(f"❌ <b>Connection Failed!</b>\nError: {html.escape(str(e))}"))

    elif data == "TGZ_control":
        if chat_id in user_states: del user_states[chat_id]
        edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())

    elif data == "TGZ_toggle_w":
        bot_settings["withdraw_on"] = not bot_settings["withdraw_on"]
        save_db()
        edit_message(chat_id, msg_id, render_body_text("🕹 <b>TGZ CONTROL PANEL</b>"), reply_markup=TGZ_control_keyboard())

    elif data == "manage_w_methods":
        edit_message(chat_id, msg_id, render_body_text("💳 <b>WITHDRAWAL METHODS</b>\n\nManage your withdrawal methods below:"), reply_markup=w_methods_keyboard())

    elif data == "add_wm":
        user_states[chat_id] = "wait_for_add_wm"
        temp_data[chat_id] = {"msg_id": msg_id}
        edit_message(chat_id, msg_id, render_body_text("📝 Send the name of the new Withdrawal Method:"), reply_markup={"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "manage_w_methods", "style": "danger"}]]})

    elif data.startswith("del_wm_"):
        idx = int(data.split("_")[2])
        if 0 <= idx < len(bot_settings["w_methods"]):
            del bot_settings["w_methods"][idx]
            save_db()
            answer_callback(call["id"], "✅ Method deleted!", show_alert=True)
            edit_message(chat_id, msg_id, render_body_text("💳 <b>WITHDRAWAL METHODS</b>\n\nManage your withdrawal methods below:"), reply_markup=w_methods_keyboard())

    elif data.startswith("TGZ_"):
        key = data.replace("TGZ_", "")
        key_map = {"min_w": "min_withdraw", "otp_r": "otp_reward", "ref_r": "refer_reward", "cool": "cooldown", "num_req": "num_req", "num_share": "num_share", "sup_link": "support_link", "w_group": "w_group"}
        if key in key_map:
            temp_data[chat_id] = {"msg_id": msg_id, "key": key_map[key]}
            user_states[chat_id] = "set_TGZ"
            cancel_kb = {"inline_keyboard": [[{"text": "Cancel", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "cancel_TGZ_edit", "style": "danger"}]]}
            prompt = f"📝 Please send the new value for <code>{key_map[key]}</code>:"
            edit_message(chat_id, msg_id, render_body_text(prompt), reply_markup=cancel_kb)
            answer_callback(call["id"])

    elif data == "back_to_services":
        local_srvs = set([b["service"] for b in number_batches.values() if b["numbers"]])
        stex_srvs = set(bot_settings.get("stex_services", {}).keys())
        voltx_srvs = set(bot_settings.get("voltx_services", {}).keys())
        zebrasms_srvs = set(bot_settings.get("zebrasms_services", {}).keys())
        yesms_srvs = set(bot_settings.get("yesms_services", {}).keys())
        all_services = local_srvs.union(stex_srvs).union(voltx_srvs).union(zebrasms_srvs).union(yesms_srvs)
        
        if not all_services:
            answer_callback(call["id"], "❌ No numbers or services available!", show_alert=True)
            return
            
        c_msg = bot_settings["custom_messages"].get("get_number", {})
        txt = render_body_text(c_msg.get("text", f"{PEM['pin']} Select Service"))
        
        apps_db = bot_settings.get("premium_apps", {})
        kb = []
        for s in all_services:
            emoji_id = "5352694861990501856" 
            for app_key, app_data in apps_db.items():
                if s.upper() == app_key or s.upper() in app_key or app_key in s.upper():
                    if "id" in app_data:
                        emoji_id = app_data["id"]
                        break
            kb.append([{"text": f"{s}", "icon_custom_emoji_id": emoji_id, "callback_data": f"g_s_{s}", "style": "primary"}])
        
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
        kb.append([{"text": "Close", "icon_custom_emoji_id": "5420130255174145507", "callback_data": "close_msg", "style": "danger"}])
        
        edit_message(chat_id, msg_id, txt, reply_markup={"inline_keyboard": kb})

    elif data.startswith("g_s_"):
        service = data.split("g_s_")[1]
        local_cnts = set([b["country"] for b in number_batches.values() if b["service"] == service and b["numbers"]])
        stex_cnts = set(bot_settings.get("stex_services", {}).get(service, {}).keys())
        voltx_cnts = set(bot_settings.get("voltx_services", {}).get(service, {}).keys())
        zebrasms_cnts = set(bot_settings.get("zebrasms_services", {}).get(service, {}).keys())
        yesms_cnts = set(bot_settings.get("yesms_services", {}).get(service, {}).keys())
        all_countries = local_cnts.union(stex_cnts).union(voltx_cnts).union(zebrasms_cnts).union(yesms_cnts)
        
        c_msg = bot_settings["custom_messages"].get("select_country", {})
        raw_txt = c_msg.get("text", "📌 Select a country for {service}:").replace("{service}", service)
        txt = render_body_text(raw_txt)
        
        kb = []
        for c in all_countries:
            _, emoji_id = get_country_flag_info(c)
            if not emoji_id:
                emoji_id = "5780471598922337683"
            
            otp_price = bot_settings.get("otp_reward", 0.0)
            # 🌟 ম্যানুয়ালি অ্যাড করা Voltx/StexSMS/Zebrasms/Yesms সার্ভিসে কাস্টম রেট সেট করা থাকলে সেটা নিবে (Auto Range গুলো এডমিনের গ্লোবাল OTP REWARD রেটই ব্যবহার করবে)
            if c in stex_cnts:
                custom_rate = bot_settings.get("stex_service_rates", {}).get(service, {}).get(c)
                if custom_rate is not None: otp_price = custom_rate
            elif c in voltx_cnts:
                custom_rate = bot_settings.get("voltx_service_rates", {}).get(service, {}).get(c)
                if custom_rate is not None: otp_price = custom_rate
            elif c in zebrasms_cnts:
                custom_rate = bot_settings.get("zebrasms_service_rates", {}).get(service, {}).get(c)
                if custom_rate is not None: otp_price = custom_rate
            elif c in yesms_cnts:
                custom_rate = bot_settings.get("yesms_service_rates", {}).get(service, {}).get(c)
                if custom_rate is not None: otp_price = custom_rate
            # 🌟 ফাইলের নির্দিষ্ট কোনো রেট থাকলে সেটা নিয়ে আসবে (সবচেয়ে বেশি প্রায়রিটি)
            for b in number_batches.values():
                if b.get("service") == service and b.get("country") == c and "rate" in b and b.get("numbers"):
                    otp_price = b.get("rate")
                    break
                    
            kb.append([{"text": f"{c} - {otp_price} TK/OTP", "icon_custom_emoji_id": emoji_id, "callback_data": f"g_c_{service}_{c}", "style": "success"}])
        
        
        for b in c_msg.get("buttons", []): 
            b_copy = b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
            
        kb.append([{"text": "Back", "icon_custom_emoji_id": "5267490665117275176", "callback_data": "back_to_services", "style": "danger"}])
        edit_message(chat_id, msg_id, txt, reply_markup={"inline_keyboard": kb})

    elif data.startswith("g_c_") or data.startswith("c_n_"):
        # ১. গ্লোবাল কুলডাউন চেক (সকল নাম্বার মেথডের জন্য)
        now = time.time()
        if now - user_cooldowns.get(chat_id, 0) < bot_settings["cooldown"]:
            answer_callback(call["id"], f"⌛ Please wait {int(bot_settings['cooldown'] - (now - user_cooldowns.get(chat_id, 0)))}s.", show_alert=True)
            return
        
        # কুলডাউন আপডেট
        user_cooldowns[chat_id] = now
        
        # আগের নাম্বার এক্সপায়ার করা
        expire_previous_number(chat_id)

        # যদি সার্চ নাম্বার থেকে আসে
        if data.startswith("c_n_s_"):
            is_voltx_req = data.endswith("_vtx")
            is_zebrasms_req = data.endswith("_zbs")
            is_yesms_req = data.endswith("_ysm")
            clean_data = data[:-4] if (is_voltx_req or is_zebrasms_req or is_yesms_req) else data
            parts_s = clean_data.split("_", 4)
            
            query = parts_s[3] if len(parts_s) > 3 else ""
            service_from_cb = parts_s[4] if len(parts_s) > 4 else None
            
            # 🌟 আগে Search Countries লিস্ট থেকে চেক করা হতো; এখন যেহেতু এই হ্যান্ডলারটি শুধুমাত্র
            # ম্যানুয়ালি অ্যাড করা সার্ভিস/রেঞ্জ থেকেই কল হয়, তাই vtx/zbs/ysm ফ্ল্যাগ অনুযায়ী প্যানেল নির্ধারণ হবে
            is_stex_allowed = not is_voltx_req and not is_zebrasms_req and not is_yesms_req
            is_voltx_allowed = is_voltx_req
            is_zebrasms_allowed = is_zebrasms_req
            is_yesms_allowed = is_yesms_req
                
            edit_message(chat_id, msg_id, render_body_text("⌛ <i>Processing... Finding Number...</i>"))
            wait_msg_id = msg_id
            
            found_indices = []
            for b_id, b_data in number_batches.items():
                for idx, n_obj in enumerate(b_data["numbers"]):
                    if n_obj["num"].replace("+", "").startswith(query) and chat_id not in n_obj.get("used_by", []):
                        found_indices.append((b_id, idx))
            
            fetched_nums = []
            if not found_indices:
                api_found = False
                req_count = bot_settings.get("num_req", 1)
                
                # প্রথমে Zebrasms চেক করবে
                if is_zebrasms_allowed or is_zebrasms_req:
                    zebrasms_keys = bot_settings.get("zebrasms_keys", [])
                    for _ in range(req_count):
                        if len(fetched_nums) >= req_count: break
                        for api_key in zebrasms_keys:
                            try:
                                headers = {"MAuth": api_key, "Content-Type": "application/json"}
                                payload = {"range": query}
                                res = requests.post(f"{ZEBRASMS_BASE_URL}/publicapi/getnum", json=payload, headers=headers, timeout=10)
                                resp_data = res.json()
                                if resp_data.get("meta", {}).get("code") == 0 and resp_data.get("data", {}).get("rows"):
                                    row0 = resp_data["data"]["rows"][0]
                                    num_str = str(row0.get("number", "")).replace("+", "")
                                    fetched_nums.append(num_str)
                                    zebrasms_assigned_numbers[num_str] = chat_id 
                                    assigned_number_rates[num_str] = resolve_manual_service_rate("zebrasms", service_from_cb, query)
                                    api_found = True
                                    total_assigned_stats += 1
                                    is_zebrasms_req = True 
                                    break
                            except: continue

                # তারপর Voltx চেক করবে
                if len(fetched_nums) < req_count and (is_voltx_allowed or is_voltx_req) and not is_zebrasms_req:
                    voltx_keys = bot_settings.get("voltx_keys", [])
                    for _ in range(req_count):
                        if len(fetched_nums) >= req_count: break
                        for api_key in voltx_keys:
                            try:
                                headers = {"mauthapi": api_key}
                                payload = {"rid": query}
                                res = requests.post(f"{VOLTX_BASE_URL}/getnum", json=payload, headers=headers, timeout=10)
                                resp_data = res.json()
                                if resp_data.get("meta", {}).get("code") == 200 and resp_data.get("data"):
                                    num_str = str(resp_data["data"].get("no_plus_number", "")).replace("+", "")
                                    if not num_str: num_str = str(resp_data["data"].get("national_number", ""))
                                    fetched_nums.append(num_str)
                                    voltx_assigned_numbers[num_str] = chat_id 
                                    assigned_number_rates[num_str] = resolve_manual_service_rate("voltx", service_from_cb, query)
                                    api_found = True
                                    total_assigned_stats += 1
                                    is_voltx_req = True # মার্ক করে দিলাম যাতে পরবর্তীতে Voltx রিকোয়েস্ট হিসেবে কাজ করে
                                    break
                            except: continue

                # Voltx এ না পেলে বা আরও নাম্বার লাগলে StexSMS তে চেক করবে
                if len(fetched_nums) < req_count and (is_stex_allowed and not is_voltx_req and not is_zebrasms_req and not is_yesms_req):
                    stex_keys = bot_settings.get("stex_keys", [])
                    for _ in range(req_count - len(fetched_nums)):
                        for api_key in stex_keys:
                            try:
                                headers = {"mauthapi": api_key}
                                res = requests.post(f"{STEX_BASE_URL}/getnum", json={"rid": query}, headers=headers, timeout=10)
                                resp_data = res.json()
                                if resp_data.get("meta", {}).get("code") == 200 and resp_data.get("data"):
                                    num_str = str(resp_data["data"].get("no_plus_number", "")).replace("+", "")
                                    if not num_str: num_str = str(resp_data["data"].get("national_number", ""))
                                    fetched_nums.append(num_str)
                                    stex_assigned_numbers[num_str] = chat_id 
                                    assigned_number_rates[num_str] = resolve_manual_service_rate("stex", service_from_cb, query)
                                    api_found = True
                                    total_assigned_stats += 1
                                    break
                            except: continue

                # Yesms চেক করবে
                if len(fetched_nums) < req_count and (is_yesms_allowed or is_yesms_req):
                    yesms_keys = bot_settings.get("yesms_keys", [])
                    for _ in range(req_count - len(fetched_nums)):
                        for api_key in yesms_keys:
                            try:
                                headers = {"authkey": api_key, "Content-Type": "application/json"}
                                res = requests.post(f"{YESMS_BASE_URL}/allocate_number", json={"range_id": query}, headers=headers, timeout=10)
                                resp_data = res.json()
                                if resp_data.get("success") and resp_data.get("data"):
                                    country_hint = provider_country("yesms", service_from_cb, query)
                                    raw_num = resp_data["data"].get("full_number", "")
                                    if not raw_num:
                                        raw_num = resp_data["data"].get("national_number", "")
                                    num_str = normalize_provider_number(raw_num, country_hint)
                                    fetched_nums.append(num_str)
                                    yesms_assigned_numbers[num_str] = chat_id 
                                    assigned_number_rates[num_str] = resolve_manual_service_rate("yesms", service_from_cb, query)
                                    api_found = True
                                    total_assigned_stats += 1
                                    is_yesms_req = True
                                    break
                            except: continue
                        
                if not api_found:
                    answer_callback(call["id"], "❌ Number out of stock!", show_alert=True)
                    delete_message(chat_id, wait_msg_id)
                    return
                save_db()
            else:
                random.shuffle(found_indices)
                for b_id, idx in found_indices:
                    if len(fetched_nums) >= bot_settings.get("num_req", 1): break
                    n_obj = number_batches[b_id]["numbers"][idx]
                    num_str = n_obj["num"]
                    fetched_nums.append(num_str)
                    
                    # 🌟 Save File Specific Rate
                    assigned_number_rates[num_str] = float(number_batches[b_id].get("rate", bot_settings.get("otp_reward", 0.0)))
                    
                    n_obj["shares"] += 1
                    n_obj["used_by"].append(chat_id)
                    total_assigned_stats += 1
                    if n_obj["shares"] >= bot_settings.get("num_share", 1):
                        n_obj["to_remove"] = True
                        used_numbers_list.append(num_str)
                for b_id in number_batches:
                    number_batches[b_id]["numbers"] = [n for n in number_batches[b_id]["numbers"] if not n.get("to_remove")]
                save_db()
                
            kb = []
            if service_from_cb:
                app_full_name, _ = get_service_info_html(service_from_cb)
                emoji_id_srv = "5337302974806922068"
                for app_key, app_data in bot_settings.get("premium_apps", {}).items():
                    if service_from_cb.upper() == app_key or service_from_cb.upper() in app_key or app_key in service_from_cb.upper():
                        if "id" in app_data: emoji_id_srv = app_data["id"]; break
                kb.append([{"text": f"{app_full_name}", "icon_custom_emoji_id": emoji_id_srv, "callback_data": "ignore", "style": "primary"}])

            flags_db = bot_settings.get("premium_flags", {})
            country_emoji_html = ""
            for num in fetched_nums:
                char_flag, iso = get_flag_and_code(num)
                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                flag_emoji_id = "5780471598922337683"
                for flag_code, flag_data in flags_db.items():
                    if iso == flag_data.get("iso"):
                        if "id" in flag_data: 
                            flag_emoji_id = flag_data["id"]
                            country_emoji_html = f'<tg-emoji emoji-id="{flag_emoji_id}">{flag_data["char"]}</tg-emoji>'
                        break
                if not country_emoji_html: country_emoji_html = char_flag
                kb.append([{"text": f"{display_num}", "icon_custom_emoji_id": flag_emoji_id, "copy_text": {"text": display_num}, "style": "success"}])
            
            vtx_ext = "_vtx" if is_voltx_req else "_zbs" if is_zebrasms_req else "_ysm" if is_yesms_req else ""
            srv_ext = f"_{service_from_cb}" if service_from_cb else ""
            kb.append([{"text": "Change Number", "icon_custom_emoji_id": "5465368548702446780", "callback_data": f"c_n_s_{query}{srv_ext}{vtx_ext}", "style": "danger"}])
            
            if service_from_cb:
                kb.append([{"text": "Change Country", "icon_custom_emoji_id": "5305517382138112561", "callback_data": f"g_s_{service_from_cb}", "style": "danger"}])
                
            kb.append([{"text": "OTP Group", "icon_custom_emoji_id": "5190447043545438788", "url": bot_settings["otp_link"], "style": "primary"}])
            
            text_numbers = render_body_text(f"{country_emoji_html} NEW NUMBER\n")
            # Change Number করলে পুরনো মেসেজ ডিলিট হয়ে নতুন মেসেজে নতুন নাম্বার আসবে
            delete_message(chat_id, wait_msg_id)
            msg_res = send_message(chat_id, text_numbers, reply_markup={"inline_keyboard": kb})
            new_msg_id = msg_res["result"]["message_id"] if msg_res and "result" in msg_res else wait_msg_id
            user_active_sessions[chat_id] = {
                "msg_id": new_msg_id, "nums": fetched_nums,
            }
            return

        # যদি আপলোড করা বা সার্ভিস থেকে আসে
        parts = data.split("_")
        service = parts[2]
        country = parts[3]

        available_indices = []
        # Check Local Stock First
        for b_id, b_data in number_batches.items():
            if b_data["service"] == service and b_data["country"] == country:
                for idx, n_obj in enumerate(b_data["numbers"]):
                    if chat_id not in n_obj.get("used_by", []):
                        available_indices.append((b_id, idx))

        # IF NO LOCAL STOCK, Check StexSMS, Voltx, Zebrasms & Yesms Services
        if not available_indices:
            stex_srv_data = bot_settings.get("stex_services", {}).get(service, {}).get(country)
            voltx_srv_data = bot_settings.get("voltx_services", {}).get(service, {}).get(country)
            zebrasms_srv_data = bot_settings.get("zebrasms_services", {}).get(service, {}).get(country)
            yesms_srv_data = bot_settings.get("yesms_services", {}).get(service, {}).get(country)
            
            target_range = None
            is_voltx = False
            is_zebrasms = False
            is_yesms = False
            
            if stex_srv_data and len(stex_srv_data) > 0:
                target_range = random.choice(stex_srv_data)
            elif voltx_srv_data and len(voltx_srv_data) > 0:
                target_range = random.choice(voltx_srv_data)
                is_voltx = True
            elif zebrasms_srv_data and len(zebrasms_srv_data) > 0:
                target_range = random.choice(zebrasms_srv_data)
                is_zebrasms = True
            elif yesms_srv_data and len(yesms_srv_data) > 0:
                target_range = random.choice(yesms_srv_data)
                is_yesms = True
                
            if target_range:
                user_cooldowns[chat_id] = 0
                vtx_flag = "_vtx" if is_voltx else "_zbs" if is_zebrasms else "_ysm" if is_yesms else ""
                handle_callback({"message": call["message"], "data": f"c_n_s_{target_range}_{service}{vtx_flag}", "id": call["id"]})
                return
            else:
                answer_callback(call["id"], "❌ Number out of stock or range missing!", show_alert=True)
                if data.startswith("c_n_"): delete_message(chat_id, msg_id)
                return

        random.shuffle(available_indices)
        
        fetched_nums = []
        for b_id, idx in available_indices:
            if len(fetched_nums) >= bot_settings["num_req"]: break
            n_obj = number_batches[b_id]["numbers"][idx]
            
            fetched_nums.append(n_obj["num"])
            
            # 🌟 Save File Specific Rate
            assigned_number_rates[n_obj["num"]] = float(number_batches[b_id].get("rate", bot_settings.get("otp_reward", 0.0)))
            
            n_obj["shares"] += 1
            n_obj["used_by"].append(chat_id)
            total_assigned_stats += 1
            
            if n_obj["shares"] >= bot_settings.get("num_share", 1):
                n_obj["to_remove"] = True
                used_numbers_list.append(n_obj["num"])

        for b_id in number_batches:
            number_batches[b_id]["numbers"] = [n for n in number_batches[b_id]["numbers"] if not n.get("to_remove")]
        save_db()

        if not fetched_nums:
            answer_callback(call["id"], "❌ You have already taken all numbers or stock is empty!", show_alert=True)
            if data.startswith("c_n_"): delete_message(chat_id, msg_id)
            return

        app_full_name, _ = get_service_info_html(service)
        emoji_id = "5337302974806922068"
        apps_db = bot_settings.get("premium_apps", {})
        for app_key, app_data in apps_db.items():
            if service.upper() == app_key or service.upper() in app_key or app_key in service.upper():
                if "id" in app_data:
                    emoji_id = app_data["id"]
                    break
        kb = [[{"text": f"{app_full_name}", "icon_custom_emoji_id": emoji_id, "callback_data": "ignore", "style": "primary"}]]
        
        flags_db = bot_settings.get("premium_flags", {})
        country_emoji_html = ""
        for num in fetched_nums:
            char_flag, iso = get_flag_and_code(num)
            display_num = f"+{num}" if not num.startswith("+") else num
            
            flag_emoji_id = "5780471598922337683" # Default Flag
            for flag_code, flag_data in flags_db.items():
                if iso == flag_data.get("iso"):
                    if "id" in flag_data: 
                        flag_emoji_id = flag_data["id"]
                        country_emoji_html = f'<tg-emoji emoji-id="{flag_emoji_id}">{flag_data["char"]}</tg-emoji>'
                    break
            
            if not country_emoji_html: country_emoji_html = char_flag
            kb.append([{"text": f"{display_num}", "icon_custom_emoji_id": flag_emoji_id, "copy_text": {"text": display_num}, "style": "success"}])
            
        kb.append([{"text": "Change Number", "icon_custom_emoji_id": "5465368548702446780", "callback_data": f"c_n_{service}_{country}", "style": "danger"}])
                   
        c_btns = bot_settings["custom_messages"].get("get_number", {}).get("buttons", [])
        for c_b in c_btns: 
            b_copy = c_b.copy()
            if "style" not in b_copy: b_copy["style"] = "primary"
            kb.append([b_copy])
            
        kb.append([{"text": "Change Country", "icon_custom_emoji_id": "5305517382138112561", "callback_data": f"g_s_{service}", "style": "danger"}])
        kb.append([{"text": "OTP Group", "icon_custom_emoji_id": "5190447043545438788", "url": bot_settings["otp_link"], "style": "primary"}])
        
        text_numbers = render_body_text(f"{country_emoji_html} NEW NUMBER\n")
        # Change Number করলে পুরনো মেসেজ ডিলিট হয়ে নতুন মেসেজে নতুন নাম্বার আসবে
        delete_message(chat_id, msg_id)
        msg_res = send_message(chat_id, text_numbers, reply_markup={"inline_keyboard": kb})
        if msg_res and "result" in msg_res:
            user_active_sessions[chat_id] = {
                "msg_id": msg_res["result"]["message_id"], "nums": fetched_nums,
            }

    elif data.startswith("wapp_") or data.startswith("wrej_"):
        # অ্যাডমিন চেক (User ID চেক করতে হবে)
        user_id_clicked = call["from"]["id"]
        if not is_admin(user_id_clicked):
            answer_callback(call["id"], "🚫 Only Bot Admins can process withdrawals!", show_alert=True)
            return
            
        action = "APPROVE" if data.startswith("wapp_") else "REJECT"
        req_id = data.replace("wapp_", "").replace("wrej_", "")
        
        if req_id in pending_withdrawals:
            req_data = pending_withdrawals[req_id]
            u_id, amt = req_data["user_id"], req_data["amount"]
            num = req_data["number"]
            full_name = req_data.get("full_name", u_id)
            
            if action == "APPROVE" and len(num) >= 7:
                masked_num = f"{num[:4]}❖TGZ❖{num[-3:]}"
            else:
                masked_num = num
            
            status_text = "APPROVED" if action == "APPROVE" else "REJECTED"
            emoji_icon_id = "5352694861990501856" if action == "APPROVE" else "5420130255174145507"
            new_text = f"🎙 <b>WITHDRAWAL {status_text}</b>\n\n👤 <b>USER:</b> <a href='tg://user?id={u_id}'>{full_name}</a>\n💳 <b>WITHDRAWAL:</b> {amt} TK\n🍏 <b>NUMBER:</b> <code>{masked_num}</code>\n🏦 <b>METHOD:</b> {req_data['method']}\n\n🧾 <b>REQ ID:</b> {req_id}\n👨‍⚖️ <b>PROCESSED BY ADMIN</b>"
            
            kb = {"inline_keyboard": [[{"text": status_text, "icon_custom_emoji_id": emoji_icon_id, "callback_data": "ignore", "style": "success" if action == "APPROVE" else "danger"}]]}
            edit_message(chat_id, msg_id, render_body_text(new_text), reply_markup=kb)
            
            if action == "REJECT":
                update_balance(u_id, amt) 
                send_message(u_id, render_body_text(f"❌ Your {amt} TK withdrawal request was rejected. Balance refunded."))
            else:
                send_message(u_id, render_body_text(f"{PEM['ok']} Your {amt} TK withdrawal request has been paid successfully!"))
            
            if db:
                try: db.collection('withdrawals').document(req_id).update({"status": "approved" if action == "APPROVE" else "rejected"})
                except: pass
                
            del pending_withdrawals[req_id]
        else:
            answer_callback(call["id"], "❌ Request already processed!", show_alert=True)

# ==========================================
# Polling Loop
# ==========================================
def poll_otp_with_status(number_id, num_str, owner_id, api_key):
    headers = {"X-API-Key": api_key}
    for _ in range(150): # 150 * 4 seconds = 10 Minutes Polling
        try:
            res = requests.get(f"{STEX_BASE_URL}/api/v1/numbers/{number_id}/sms", headers=headers, timeout=10)
            data = res.json()
            if data.get("success") and data.get("otp"):
                otp = str(data["otp"])
                msg_text = data.get("message", f"Your code is {otp}")
                
                # 🌟 সম্পূর্ণ মেসেজ থেকে ড্যাশসহ বা বড় OTP খোঁজার ফিক্স
                extracted_otp = extract_otp_code(msg_text)
                if extracted_otp and len(extracted_otp) > len(otp):
                    otp = extracted_otp
                    
                # 🌟 সম্পূর্ণ মেসেজ থেকে সার্ভিস/অ্যাপ চেনার ফিক্স
                app_name = data.get("service", "StexSMS Service")
                detected_app = detect_service(msg_text)
                if detected_app:
                    app_name = detected_app
                
                if claim_processed_otp(num_str, otp, [f"POLL_{number_id}_{otp}"]):
                    
                    char, iso = get_flag_and_code(num_str)
                    app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                    
                    save_db()
                    
                    display_num = f"+{num_str}" if not str(num_str).startswith("+") else str(num_str)
                    masked = mask_number(display_num)
                    lang = detect_language(msg_text)
                    
                    display_msg = render_body_text(f"╔═══════════════╗\n║ {prem_app_html} {get_flag_info_html(display_num)} {masked} {lang}\n╚═══════════════╝")
                    
                    for fw in bot_settings.get("fw_groups", []):
                        kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                        temp_row = []
                        styles = ["danger", "success", "primary"]
                        for i, btn in enumerate(fw.get("buttons", [])):
                            b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                            if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                            temp_row.append(b_obj)
                            if len(temp_row) == 2:
                                kb.append(temp_row)
                                temp_row = []
                        if temp_row: kb.append(temp_row)
                        send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                    
                    inbox_msg = render_body_text(f"╔═══════════════╗\n║ {prem_app_html} {get_flag_info_html(display_num)} {display_num} {lang}\n╚═══════════════╝")
                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                    
                    reward = float(assigned_number_rates.get(num_str, bot_settings.get("otp_reward", 0.0)))
                    if reward > 0:
                        update_balance(owner_id, reward)
                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                    
                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                    
                    if db:
                        try: db.collection('users').document(str(owner_id)).update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1)})
                        except: pass
                break
        except: pass
        time.sleep(4)

def voltx_sms_listener():
    global processed_otps, voltx_assigned_numbers
    while True:
        try:
            voltx_keys = bot_settings.get("voltx_keys", [])
            for api_key in voltx_keys:
                try:
                    headers = {"mauthapi": api_key}
                    res = requests.get(f"{VOLTX_BASE_URL}/success-otp", headers=headers, timeout=10)
                    resp_data = res.json()
                    
                    if resp_data.get("meta", {}).get("code") == 200 and "data" in resp_data and "otps" in resp_data["data"]:
                        for item in resp_data["data"]["otps"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("message", ""))
                            otp = extract_otp_code(msg_text) or "CODE"
                            # 🌟 FIX: dedup by the actual OTP code, not the panel's
                            # otp_id/record id (which is different every fetch even
                            # when the OTP text itself repeats) - so a repeated OTP
                            # is correctly skipped, but a different OTP still comes
                            # through.
                            app_name = "Voltx Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app
                                
                            if claim_processed_otp(num, otp) and num:
                                
                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()
                                
                                save_db()
                                
                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)
                                
                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
                                
                                for fw in bot_settings.get("fw_groups", []):
                                    kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                                    temp_row = []
                                    styles = ["danger", "success", "primary"]
                                    for i, btn in enumerate(fw.get("buttons", [])):
                                        b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                                        if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                                        temp_row.append(b_obj)
                                        if len(temp_row) == 2:
                                            kb.append(temp_row)
                                            temp_row = []
                                    if temp_row: kb.append(temp_row)
                                    send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                                    
                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                
                                for uid, session_data in list(user_active_sessions.items()):
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break
                                    
                                if not owner_id:
                                    for vtx_n, n_owner in voltx_assigned_numbers.items():
                                        clean_vtx = str(vtx_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_vtx == clean_api_num or (len(clean_vtx) >= 8 and clean_vtx.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_vtx[-8:])):
                                            owner_id = n_owner
                                            break
                                        
                                if owner_id:
                                    lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                                    
                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                                    
                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                                    
                                    if db:
                                        try: 
                                            db.collection('users').document(str(owner_id)).update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1)})
                                            if owner_id in user_cache:
                                                user_cache[owner_id]["total_otps"] = user_cache[owner_id].get("total_otps", 0) + 1
                                        except: pass
                except: pass
        except: pass
        time.sleep(5)

def zebrasms_sms_listener():
    global processed_otps, zebrasms_assigned_numbers
    while True:
        try:
            zebrasms_keys = bot_settings.get("zebrasms_keys", [])
            for api_key in zebrasms_keys:
                try:
                    headers = {"MAuth": api_key}
                    res = requests.get(f"{ZEBRASMS_BASE_URL}/publicapi/getupdate", headers=headers, timeout=10)
                    resp_data = res.json()
                    
                    if resp_data.get("meta", {}).get("code") == 0 and "data" in resp_data and "rows" in resp_data["data"]:
                        for item in resp_data["data"]["rows"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("message", ""))
                            otp = extract_otp_code(msg_text) or "CODE"
                            # 🌟 FIX: dedup by number+OTP only. Appending the
                            # millisecond timestamp made every fetch "unique" even
                            # when the same OTP repeated, so duplicates were never
                            # actually blocked.
                            app_name = "Zebra Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app
                                
                            if claim_processed_otp(num, otp) and num:
                                
                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()
                                
                                save_db()
                                
                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)
                                
                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
                                
                                for fw in bot_settings.get("fw_groups", []):
                                    kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                                    temp_row = []
                                    styles = ["danger", "success", "primary"]
                                    for i, btn in enumerate(fw.get("buttons", [])):
                                        b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                                        if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                                        temp_row.append(b_obj)
                                        if len(temp_row) == 2:
                                            kb.append(temp_row)
                                            temp_row = []
                                    if temp_row: kb.append(temp_row)
                                    send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                                    
                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                
                                for uid, session_data in list(user_active_sessions.items()):
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break
                                    
                                if not owner_id:
                                    for zbs_n, n_owner in zebrasms_assigned_numbers.items():
                                        clean_zbs = str(zbs_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_zbs == clean_api_num or (len(clean_zbs) >= 8 and clean_zbs.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_zbs[-8:])):
                                            owner_id = n_owner
                                            break
                                        
                                if owner_id:
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                                    
                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                                    
                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                                    
                                    if db:
                                        try: 
                                            db.collection('users').document(str(owner_id)).update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1)})
                                            if owner_id in user_cache:
                                                user_cache[owner_id]["total_otps"] = user_cache[owner_id].get("total_otps", 0) + 1
                                        except: pass
                except: pass
        except: pass
        time.sleep(5)

def yesms_sms_listener():
    global processed_otps, yesms_assigned_numbers
    while True:
        try:
            yesms_keys = bot_settings.get("yesms_keys", [])
            for api_key in yesms_keys:
                try:
                    headers = {"authkey": api_key}
                    res = requests.get(f"{YESMS_BASE_URL}/user_numbers", headers=headers, timeout=10)
                    resp_data = res.json()
                    
                    if resp_data.get("success") and "logs" in resp_data:
                        for item in resp_data["logs"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("full_message", ""))
                            otp = str(item.get("otp_code", "")) or extract_otp_code(msg_text) or "CODE"
                            # 🌟 FIX: dedup by number+OTP only, same reasoning as
                            # Zebrasms above - the timestamp was breaking dedup.
                            app_name = "Yesms Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app
                                
                            if claim_processed_otp(num, otp) and num:
                                
                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()
                                
                                save_db()
                                
                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)
                                
                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
                                
                                for fw in bot_settings.get("fw_groups", []):
                                    kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                                    temp_row = []
                                    styles = ["danger", "success", "primary"]
                                    for i, btn in enumerate(fw.get("buttons", [])):
                                        b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                                        if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                                        temp_row.append(b_obj)
                                        if len(temp_row) == 2:
                                            kb.append(temp_row)
                                            temp_row = []
                                    if temp_row: kb.append(temp_row)
                                    send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                                    
                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                
                                for uid, session_data in list(user_active_sessions.items()):
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break
                                    
                                if not owner_id:
                                    for ysm_n, n_owner in yesms_assigned_numbers.items():
                                        clean_ysm = str(ysm_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_ysm == clean_api_num or (len(clean_ysm) >= 8 and clean_ysm.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_ysm[-8:])):
                                            owner_id = n_owner
                                            break
                                        
                                if owner_id:
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                                    
                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                                    
                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                                    
                                    if db:
                                        try: 
                                            db.collection('users').document(str(owner_id)).update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1)})
                                            if owner_id in user_cache:
                                                user_cache[owner_id]["total_otps"] = user_cache[owner_id].get("total_otps", 0) + 1
                                        except: pass
                except: pass
        except: pass
        time.sleep(5)

def global_sms_listener():
    global processed_otps, stex_assigned_numbers
    while True:
        try:
            stex_keys = bot_settings.get("stex_keys", [])
            for api_key in stex_keys:
                try:
                    headers = {"mauthapi": api_key}
                    res = requests.get(f"{STEX_BASE_URL}/success-otp", headers=headers, timeout=10)
                    resp_data = res.json()
                    
                    if resp_data.get("meta", {}).get("code") == 200 and "data" in resp_data and "otps" in resp_data["data"]:
                        for item in resp_data["data"]["otps"]:
                            num = str(item.get("number", "")).replace("+", "")
                            msg_text = str(item.get("message", ""))
                            otp = extract_otp_code(msg_text) or "CODE"
                            # 🌟 FIX: dedup by the actual OTP code, not the panel's
                            # otp_id/record id, same reasoning as Voltx above.
                            app_name = "Stex Service"
                            detected_app = detect_service(msg_text)
                            if detected_app: app_name = detected_app
                                
                            if claim_processed_otp(num, otp) and num:
                                
                                char, iso = get_flag_and_code(num)
                                app_full_name, prem_app_html = get_service_info_html(app_name, msg_text)
                                current_time = time.time()
                                
                                save_db()
                                
                                display_num = f"+{num}" if not str(num).startswith("+") else str(num)
                                masked = mask_number(display_num)
                                lang = detect_language(msg_text)
                                
                                lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                display_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {masked} | 💬 {lang_name}")
                                
                                for fw in bot_settings.get("fw_groups", []):
                                    kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5296369303661067030", "copy_text": {"text": otp}, "style": "success"}]]
                                    temp_row = []
                                    styles = ["danger", "success", "primary"]
                                    for i, btn in enumerate(fw.get("buttons", [])):
                                        b_obj = {"text": btn["text"], "url": btn["url"], "style": styles[i % 3]}
                                        if "icon_custom_emoji_id" in btn: b_obj["icon_custom_emoji_id"] = btn["icon_custom_emoji_id"]
                                        temp_row.append(b_obj)
                                        if len(temp_row) == 2:
                                            kb.append(temp_row)
                                            temp_row = []
                                    if temp_row: kb.append(temp_row)
                                    send_message(fw["chat_id"], display_msg, reply_markup={"inline_keyboard": kb})
                                    
                                owner_id = None
                                clean_api_num = str(num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                
                                for uid, session_data in list(user_active_sessions.items()):
                                    for act_num in session_data.get("nums", []):
                                        act_clean = str(act_num).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if act_clean == clean_api_num or (len(act_clean) >= 8 and act_clean.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(act_clean[-8:])):
                                            owner_id = uid
                                            break
                                    if owner_id: break
                                    
                                if not owner_id:
                                    for stex_n, n_owner in stex_assigned_numbers.items():
                                        clean_stex = str(stex_n).replace("+", "").replace(" ", "").replace("-", "").strip()
                                        if clean_stex == clean_api_num or (len(clean_stex) >= 8 and clean_stex.endswith(clean_api_num[-8:])) or (len(clean_api_num) >= 8 and clean_api_num.endswith(clean_stex[-8:])):
                                            owner_id = n_owner
                                            break
                                        
                                if owner_id:
                                    lang_name = LANG_MAP.get(lang, "English") if 'LANG_MAP' in globals() else lang.replace("#", "")
                                    inbox_msg = render_body_text(f"{get_flag_info_html(display_num)} {iso} | {prem_app_html} {display_num} | 💬 {lang_name}")
                                    inbox_kb = [[{"text": f"{otp}", "icon_custom_emoji_id": "5353022963132174959", "copy_text": {"text": otp}, "style": "success"}]]
                                    
                                    reward = float(assigned_number_rates.get(num, bot_settings.get("otp_reward", 0.0)))
                                    if reward > 0:
                                        update_balance(owner_id, reward)
                                        inbox_kb.append([{"text": f"Added {reward} tk", "icon_custom_emoji_id": "5420396762189831222", "callback_data": "ignore", "style": "primary"}])
                                    
                                    send_message(owner_id, inbox_msg, reply_markup={"inline_keyboard": inbox_kb})
                                    
                                    if db:
                                        try: 
                                            db.collection('users').document(str(owner_id)).update({"total_otps": local_ops.Increment(1), "weekly_otps": local_ops.Increment(1)})
                                            if owner_id in user_cache:
                                                user_cache[owner_id]["total_otps"] = user_cache[owner_id].get("total_otps", 0) + 1
                                        except: pass
                except: pass
        except: pass
        time.sleep(5)

def main():
    global BOT_USERNAME
    res = api_call("getMe")
    if res.get("ok"): BOT_USERNAME = res["result"]["username"]
    print(f"🤖 Bot is starting... @{BOT_USERNAME}")
    
    threading.Thread(target=panel_monitor_thread, daemon=True).start()
    threading.Thread(target=global_sms_listener, daemon=True).start()
    threading.Thread(target=voltx_sms_listener, daemon=True).start()
    threading.Thread(target=zebrasms_sms_listener, daemon=True).start()
    threading.Thread(target=yesms_sms_listener, daemon=True).start()
    threading.Thread(target=start_health_check_server, daemon=True).start()
    threading.Thread(target=leaderboard_auto_refresh_thread, daemon=True).start()
    threading.Thread(target=retry_local_load, daemon=True).start()
    print("📡 Background APIs & Global SMS Listener Started!")

    # 🌟 PRO-LEVEL FAST SYSTEM: 500 Workers Pool
    # Telegram rate limits are easier to respect with a
    # bounded pool than with hundreds of simultaneous handler threads.
    executor = ThreadPoolExecutor(max_workers=32)
    
    offset = None
    while True:
        try:
            updates = api_call(f"getUpdates?timeout=50&offset={offset}")
            if updates and "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    if "message" in update:
                        executor.submit(safe_handle_message, update["message"])
                    elif "callback_query" in update:
                        executor.submit(safe_handle_callback, update["callback_query"])
        except Exception as e:
            print(f"Update polling error: {e}")
            time.sleep(2)

def safe_handle_message(message):
    try:
        handle_message(message)
    except Exception as e:
        chat_id = message.get("chat", {}).get("id")
        print(f"Message handler error for {chat_id}: {e}")
        if chat_id:
            send_message(chat_id, "সাময়িক সার্ভার সমস্যার কারণে অনুরোধটি সম্পন্ন হয়নি। একটু পরে আবার চেষ্টা করুন।")

def safe_handle_callback(callback):
    try:
        handle_callback(callback)
    except Exception as e:
        callback_id = callback.get("id")
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        print(f"Callback handler error for {chat_id}: {e}")
        if callback_id:
            answer_callback(callback_id, "সাময়িক সমস্যা হয়েছে, আবার চেষ্টা করুন।", True)

if __name__ == "__main__":
    main()
