# refdata.py — довідники для майстра фільтрів
"""
Довідкові дані: регіони України та типи процедур Prozorro.Sale.

Українські назви типів процедур узяті з nrcukraine.com.ua, щоб бот
і сайт говорили однаковою мовою.

У API поле sellingMethod має вигляд "landSell-priorityEnglish", тобто
"<група>-<різновид аукціону>". Користувач обирає саме групу — так у майстрі
20 зрозумілих пунктів замість ~60 технічних значень.
"""

# Групи процедур: код групи -> (назва українською, емодзі)
SELLING_METHOD_GROUPS = {
    "landSell": ("Земельні ділянки (продаж)", "🌾"),
    "landRental": ("Земельні ділянки (оренда)", "🌱"),
    "landArrested": ("Арештована земля", "⚖️"),
    "legitimatePropertyLease": ("Оренда держмайна", "🏛"),
    "regulationsPropertyLease": ("Оренда за регламентом", "📋"),
    "commercialPropertyLease": ("Комерційна оренда", "🏢"),
    "basicSell": ("Базова процедура продажу", "📦"),
    "commercialSell": ("Комерційні продажі", "💼"),
    "simpleSell": ("Простий продаж без аукціону", "🧾"),
    "bankRuptcy": ("Банкрутство", "📉"),
    "smallPrivatization": ("Мала приватизація", "🏘"),
    "largePrivatization": ("Велика приватизація", "🏗"),
    "armaProperty": ("Арештовані активи АРМА", "🔒"),
    "sanctionedAssets": ("Продаж санкційного майна", "🚫"),
    "alienation": ("Продаж прав відчуження", "📜"),
    "nonperformingLoans": ("Активи державних банків", "🏦"),
    "governmentFactoring": ("Факторинг", "💳"),
    "dgf": ("ФГВФО", "🏦"),
    "railwayCargo": ("Вагони", "🚃"),
    "subsoil": ("Надра", "⛏"),
}

# Регіони України (значення збігаються з items[].address.region.uk_UA)
REGIONS = [
    "Вінницька область",
    "Волинська область",
    "Дніпропетровська область",
    "Донецька область",
    "Житомирська область",
    "Закарпатська область",
    "Запорізька область",
    "Івано-Франківська область",
    "Київська область",
    "Кіровоградська область",
    "Луганська область",
    "Львівська область",
    "Миколаївська область",
    "Одеська область",
    "Полтавська область",
    "Рівненська область",
    "Сумська область",
    "Тернопільська область",
    "Харківська область",
    "Херсонська область",
    "Хмельницька область",
    "Черкаська область",
    "Чернівецька область",
    "Чернігівська область",
    "Автономна Республіка Крим",
    "Київ",
    "Севастополь",
]

# Скорочені підписи для кнопок (у Telegram кнопка вміщає небагато тексту)
REGION_SHORT = {r: r.replace(" область", "") for r in REGIONS}
REGION_SHORT["Автономна Республіка Крим"] = "АР Крим"
REGION_SHORT["Івано-Франківська область"] = "Івано-Франківська"
REGION_SHORT["Дніпропетровська область"] = "Дніпропетровська"


def group_of(selling_method):
    """landSell-priorityEnglish -> landSell"""
    if not selling_method:
        return ""
    return selling_method.split("-", 1)[0]


def group_title(code, with_emoji=True):
    """Назва групи процедур українською"""
    name, emoji = SELLING_METHOD_GROUPS.get(code, (code, "•"))
    return f"{emoji} {name}" if with_emoji else name


def selling_method_title(selling_method):
    """Повна назва типу процедури за значенням з API"""
    return group_title(group_of(selling_method))


def region_short(region):
    return REGION_SHORT.get(region, region)


def normalize_region(text):
    """
    Приводить довільний текст до канонічної назви регіону.
    'волинь', 'Волинська', 'волинська обл' -> 'Волинська область'
    """
    if not text:
        return None

    t = text.strip().lower().replace("’", "'").replace("`", "'")
    for r in REGIONS:
        rl = r.lower()
        if t == rl or t == REGION_SHORT[r].lower():
            return r
        # 'волинська', 'волинська обл', 'волинська обл.'
        base = rl.replace(" область", "")
        if t.startswith(base):
            return r
    return None
