#!/usr/bin/env python3
"""Builds the offline data for the reader app (runs inside GitHub Actions).

1. Downloads the books from Project Gutenberg and cleans them
   -> cache/books/<id>.txt (one paragraph per line) + cache/data/books.json
2. Streams the English Wiktionary extract from kaikki.org and keeps only words
   that appear in the books -> cache/data/dict.json
   {"l": {lemma: [hebrew, definition]}, "f": {form: lemma}}
3. Copies app/ + cache/ into _site/ and stamps the service-worker version.

Results in cache/ are reused between runs (actions/cache), so the heavy
download happens only when this script changes.
"""
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "cache")
SITE = os.path.join(ROOT, "_site")
UA = {"User-Agent": "english-reader-build/1.0 (personal offline reader)"}

# (Gutenberg id, level)  level: 1=קל 2=בינוני 3=מתקדם 4=מומחה
BOOKS = [
    (46, 2), (11, 2), (12, 2), (16, 2), (55, 2), (113, 2), (271, 2), (236, 2),
    (215, 2), (35, 2), (74, 2), (289, 2),
    (120, 3), (43, 3), (1661, 3), (2852, 3), (36, 3), (5230, 3), (910, 3),
    (45, 3), (514, 3), (103, 3), (64317, 3), (5200, 3), (844, 3), (174, 3),
    (1342, 4), (84, 4), (345, 4), (1260, 4), (768, 4), (1400, 4), (98, 4),
    (2701, 4),
    # adults: mystery & detective
    (244, 3), (2097, 3), (834, 3), (108, 3), (3289, 3), (863, 3), (1155, 3), (58866, 3), (1872, 3),
    (558, 3), (155, 4), (583, 4), (204, 4), (1695, 4),
    # adults: adventure
    (60, 3), (164, 3), (18857, 3), (1268, 3), (2166, 3), (139, 3), (95, 3), (421, 3),
    (1257, 4), (1184, 4), (5658, 4),
    # newer books: public domain in the USA since 2022-2026, or free licence (Little Brother, CC BY-NC-SA)
    (67098, 1), (73011, 1), (70083, 2), (77924, 2), (77602, 2),
    (69087, 3), (78220, 3), (18137, 3), (30142, 3), (77600, 4),
    # easy: short books in simple English, good for a 12-year-old
    (501, 1), (1154, 1), (11757, 1), (11339, 1), (1874, 1), (778, 1), (500, 1), (72577, 1), (69988, 1),
]

# genre: kids=ילדים ונוער, mystery=מתח ובלש, adventure=הרפתקאות, classic=קלאסיקה
GENRE = {
    **{i: "kids" for i in (11, 12, 16, 55, 113, 271, 236, 289, 74, 45, 514, 67098, 73011, 70083, 77924, 77602, 30142,
                           501, 1154, 11757, 11339, 1874, 778, 500, 72577, 69988)},
    **{i: "mystery" for i in (1661, 2852, 43, 244, 2097, 834, 108, 3289, 863, 1155, 58866, 1872, 558, 155, 583, 204, 1695,
                              69087, 78220, 77600)},
    **{i: "adventure" for i in (120, 215, 910, 103, 35, 36, 5230, 2701, 60, 164, 18857, 1268, 2166, 139, 95, 421, 1257, 1184, 5658,
                                18137)},
}

# short English title, Hebrew title, one-line Hebrew description, year
INFO = {
    46: ("A Christmas Carol", "מזמור לחג המולד", "קמצן זקן מקבל ביקור של שלוש רוחות בליל חג המולד.", 1843),
    11: ("Alice's Adventures in Wonderland", "עליסה בארץ הפלאות", "עליסה נופלת למחילת ארנב אל עולם הזוי ומשעשע.", 1865),
    12: ("Through the Looking-Glass", "מבעד למראה", "עליסה עוברת דרך המראה לארץ שבה הכול הוא משחק שחמט.", 1871),
    16: ("Peter Pan", "פיטר פן", "הילד שלא רצה להתבגר לוקח את וונדי ואחיה לארץ לעולם לא.", 1911),
    55: ("The Wonderful Wizard of Oz", "הקוסם מארץ עוץ", "סופה מעיפה את דורותי לארץ קסומה, והיא יוצאת לחפש את הקוסם.", 1900),
    113: ("The Secret Garden", "הגן הסודי", "ילדה יתומה מגלה גן נעול ומחזירה אותו, ואת עצמה, לחיים.", 1911),
    271: ("Black Beauty", "היופי השחור", "סיפור חייו של סוס, מסופר מפיו.", 1877),
    236: ("The Jungle Book", "ספר הג'ונגל", "מוגלי, ילד שגדל בין זאבים, ועוד סיפורים מהג'ונגל.", 1894),
    215: ("The Call of the Wild", "קול קדומים", "כלב בית נחטף לאלסקה של הבהלה לזהב ומגלה את הפרא שבו.", 1903),
    35: ("The Time Machine", "מכונת הזמן", "ממציא נוסע אל העתיד הרחוק ומגלה מה עלה בגורל האנושות.", 1895),
    74: ("The Adventures of Tom Sawyer", "הרפתקאות תום סוייר", "נער שובב ומלא דמיון בעיירה על גדות המיסיסיפי.", 1876),
    289: ("The Wind in the Willows", "הרוח בערבי הנחל", "חפרפרת, עכברוש, גירית וקרפד בהרפתקאות לאורך הנהר.", 1908),
    120: ("Treasure Island", "אי המטמון", "נער, מפת אוצר ופיראטים בהנהגת ג'ון סילבר.", 1883),
    43: ("Dr Jekyll and Mr Hyde", "ד\"ר ג'קיל ומר הייד", "רופא מכובד ושיקוי שמשחרר את הצד האפל שלו.", 1886),
    1661: ("The Adventures of Sherlock Holmes", "הרפתקאות שרלוק הולמס", "שתים עשרה תעלומות שהבלש פותר עם ד\"ר ווטסון.", 1892),
    2852: ("The Hound of the Baskervilles", "הכלב של בני בסקרוויל", "כלב רפאים מאיים על משפחת אצולה, ושרלוק הולמס חוקר.", 1902),
    36: ("The War of the Worlds", "מלחמת העולמות", "יצורים מהמאדים פולשים לאנגליה.", 1898),
    5230: ("The Invisible Man", "האיש הבלתי נראה", "מדען שהפך את עצמו לבלתי נראה מאבד שליטה.", 1897),
    910: ("White Fang", "ניב הלבן", "זאב-כלב פראי לומד לחיות בין בני אדם.", 1906),
    45: ("Anne of Green Gables", "אן שרלי", "יתומה פטפטנית ומלאת דמיון מגיעה בטעות לחווה כפרית.", 1908),
    514: ("Little Women", "נשים קטנות", "ארבע אחיות מתבגרות בימי מלחמת האזרחים האמריקנית.", 1868),
    103: ("Around the World in Eighty Days", "מסביב לעולם בשמונים יום", "ג'נטלמן אנגלי מתערב שיקיף את העולם בשמונים יום.", 1872),
    64317: ("The Great Gatsby", "גטסבי הגדול", "מיליונר מסתורי ואהבה אבודה בניו יורק של שנות העשרים.", 1925),
    5200: ("Metamorphosis", "הגלגול", "גרגור סמסא מתעורר בוקר אחד והוא חרק ענק.", 1915),
    844: ("The Importance of Being Earnest", "חשיבותו של להיות רציני", "קומדיה שנונה על זהויות בדויות ואהבה בחברה הגבוהה.", 1895),
    174: ("The Picture of Dorian Gray", "תמונתו של דוריאן גריי", "הדיוקן מזדקן במקומו, והוא שוקע בחיי הוללות.", 1890),
    1342: ("Pride and Prejudice", "גאווה ודעה קדומה", "אליזבת בנט ומר דארסי, ורושם ראשון שמטעה.", 1813),
    84: ("Frankenstein", "פרנקנשטיין", "מדען יוצר יצור חי ונרדף על ידי מעשה ידיו.", 1818),
    345: ("Dracula", "דרקולה", "הרוזן הערפד עובר מטרנסילבניה ללונדון.", 1897),
    1260: ("Jane Eyre", "ג'יין אייר", "אומנת יתומה, אדון אחוזה מסתורי וסוד בעליית הגג.", 1847),
    768: ("Wuthering Heights", "אנשי גבהים סואנים", "אהבה הרסנית בין קתרין להיתקליף על רמות יורקשייר.", 1847),
    1400: ("Great Expectations", "תקוות גדולות", "יתום בשם פיפ מקבל הון ממיטיב אלמוני.", 1861),
    98: ("A Tale of Two Cities", "בין שתי ערים", "לונדון ופריז בימי המהפכה הצרפתית.", 1859),
    2701: ("Moby Dick", "מובי דיק", "רב החובל אחאב רודף אחרי הלווייתן הלבן.", 1851),
    244: ("A Study in Scarlet", "חקירה בשני", "ההופעה הראשונה של שרלוק הולמס: גופה בבית נטוש ומילה כתובה בדם.", 1887),
    2097: ("The Sign of the Four", "סימן הארבעה", "אוצר הודי, ברית סודית ורצח בחדר נעול.", 1890),
    834: ("The Memoirs of Sherlock Holmes", "זיכרונות שרלוק הולמס", "עוד תעלומות של הולמס, עד העימות עם פרופסור מוריארטי.", 1893),
    108: ("The Return of Sherlock Holmes", "שובו של שרלוק הולמס", "הולמס חוזר אחרי שנחשב למת, ופותר שוב תעלומות בלונדון.", 1905),
    3289: ("The Valley of Fear", "עמק הפחד", "הודעה מוצפנת, רצח באחוזה ואגודה סודית מאמריקה.", 1915),
    863: ("The Mysterious Affair at Styles", "הפרשה המסתורית בסטיילס", "הופעת הבכורה של הרקול פוארו: הרעלה באחוזה כפרית.", 1920),
    1155: ("The Secret Adversary", "האויב הסודי", "טומי וטופנס רודפים אחרי מסמך סודי ואויב שאיש אינו יודע מיהו.", 1922),
    58866: ("The Murder on the Links", "הרצח במגרש הגולף", "פוארו נקרא לצרפת וחוקר רצח שהתגלה ליד מגרש גולף.", 1923),
    1872: ("The Red House Mystery", "תעלומת הבית האדום", "ירייה בחדר נעול בבית כפרי, ואורח סקרן מתחיל לחקור.", 1922),
    558: ("The Thirty-Nine Steps", "39 המדרגות", "אדם תמים נרדף ברחבי סקוטלנד אחרי שנקלע למזימת ריגול.", 1915),
    155: ("The Moonstone", "אבן הירח", "יהלום הודי מקולל נעלם ובבית כולם חשודים. נחשב לרומן הבלשי הראשון.", 1868),
    583: ("The Woman in White", "האישה בלבן", "פגישה לילית עם אישה מסתורית חושפת קנוניה של זהויות מוחלפות.", 1859),
    204: ("The Innocence of Father Brown", "תמימותו של האב בראון", "כומר צנוע פותר פשעים בעזרת הבנה עמוקה של נפש האדם.", 1911),
    1695: ("The Man Who Was Thursday", "האיש שהיה יום חמישי", "שוטר חודר לחבורת אנרכיסטים, ושום דבר אינו כפי שהוא נראה.", 1908),
    60: ("The Scarlet Pimpernel", "הפימפרנל האדום", "גיבור רעול זהות מבריח אצילים מהגיליוטינה של המהפכה הצרפתית.", 1905),
    164: ("Twenty Thousand Leagues Under the Sea", "עשרים אלף מיל מתחת למים", "קפטן נמו והצוללת נאוטילוס במסע במעמקי האוקיינוס.", 1870),
    18857: ("Journey to the Centre of the Earth", "מסע אל בטן האדמה", "פרופסור ואחיינו יורדים דרך הר געש אל מעמקי כדור הארץ.", 1864),
    1268: ("The Mysterious Island", "האי המסתורי", "ניצולי כדור פורח בונים חיים חדשים על אי בודד וסודי.", 1875),
    2166: ("King Solomon's Mines", "מכרות שלמה המלך", "משלחת יוצאת ללב אפריקה בחיפוש אחר אוצר אגדי.", 1885),
    139: ("The Lost World", "העולם האבוד", "משלחת מגלה רמה בדרום אמריקה שבה עדיין חיים דינוזאורים.", 1912),
    95: ("The Prisoner of Zenda", "האסיר מזנדה", "אנגלי שדומה למלך נאלץ להתחזות לו כדי להציל את הכתר.", 1894),
    421: ("Kidnapped", "חטוף", "נער נחטף לספינה ובורח עם מורד סקוטי דרך הרמות.", 1886),
    1257: ("The Three Musketeers", "שלושת המוסקטרים", "ד'ארטניאן הצעיר מצטרף לשלושה לוחמים אמיצים בצרפת של המאה ה-17.", 1844),
    1184: ("The Count of Monte Cristo", "הרוזן ממונטה כריסטו", "מלח שנכלא על לא עוול בכפו בורח, מתעשר ונוקם.", 1844),
    5658: ("Lord Jim", "לורד ג'ים", "קצין צעיר שכשל ברגע אחד מחפש גאולה באיים רחוקים.", 1900),
    67098: ("Winnie-the-Pooh", "פו הדב", "דב קטן וחביב והחברים שלו ביער מאה הדונמים.", 1926),
    73011: ("The House at Pooh Corner", "הבית בפינת פו", "עוד הרפתקאות של פו וחבריו, וכאן מצטרף גם טיגר הקופצני.", 1928),
    70083: ("The Tower Treasure", "האחים הארדי: אוצר המגדל", "שני אחים מתבגרים, בניו של בלש, פותרים שוד באחוזה עם מגדלים.", 1927),
    77924: ("The Secret of the Old Clock", "ננסי דרו: סוד השעון הישן", "ננסי דרו בת ה-16 מחפשת צוואה אבודה כדי להציל משפחה מעוני.", 1930),
    77602: ("The Hidden Staircase", "ננסי דרו: גרם המדרגות הנסתר", "קולות מסתוריים בבית ישן, וננסי יוצאת לגלות מי מסתתר שם.", 1930),
    69087: ("The Murder of Roger Ackroyd", "רצח רוג'ר אקרויד", "פוארו פורש לכפר אנגלי שקט ונקרא לפתור רצח, עם אחד הסופים המפתיעים בספרות.", 1926),
    78220: ("The Murder at the Vicarage", "רצח בבית הכומר", "גופה בחדר העבודה של הכומר, והופעת הבכורה של מיס מארפל.", 1930),
    18137: ("Little Fuzzy", "פאזי הקטן", "מחפש אוצרות על כוכב רחוק מגלה יצורים קטנים וחמודים. האם הם חכמים?", 1962),
    30142: ("Little Brother", "האח הקטן", "נער האקר בסן פרנסיסקו של ימינו נאבק במעקב ממשלתי אחרי פיגוע.", 2008),
    77600: ("The Maltese Falcon", "הנץ ממלטה", "הבלש סם ספייד נקלע למרדף אחרי פסלון יקר ערך. קלאסיקת הבלש הקשוח.", 1930),
    501: ("The Story of Doctor Dolittle", "דוקטור דוליטל", "רופא שמדבר עם בעלי חיים מפליג לאפריקה כדי להציל קופים חולים.", 1920),
    1154: ("The Voyages of Doctor Dolittle", "מסעות דוקטור דוליטל", "דוקטור דוליטל והתוכי פולינזיה יוצאים למסע ימי אל אי צף.", 1922),
    11757: ("The Velveteen Rabbit", "ארנב הקטיפה", "ארנב צעצוע רוצה מאוד להפוך לאמיתי בזכות אהבה של ילד. קצר ומרגש.", 1922),
    11339: ("Aesop's Fables", "משלי איזופוס", "עשרות משלים קצרים על בעלי חיים, כל אחד עם מוסר השכל. אפשר לקרוא אחד ביום.", 1912),
    1874: ("The Railway Children", "ילדי מסילת הברזל", "שלושה אחים עוברים לגור ליד מסילת רכבת ומגלים בה הרפתקאות וחברים.", 1906),
    778: ("Five Children and It", "חמישה ילדים והיצור", "חמישה אחים מוצאים בחול יצור קסום שמגשים משאלה אחת בכל יום, ותמיד משהו משתבש.", 1902),
    500: ("The Adventures of Pinocchio", "הרפתקאות פינוקיו", "בובת עץ שובבה שהאף שלה מתארך כשהיא משקרת, וחולמת להפוך לילד אמיתי.", 1883),
    72577: ("Bambi", "במבי", "חייו של עופר צעיר ביער, מהצעדים הראשונים ועד שהוא גדל.", 1923),
    69988: ("The House on the Cliff", "האחים הארדי: הבית על הצוק", "האחים הארדי חוקרים בית רדוף על צוק ומגלים מערות של מבריחים.", 1927),
}


def same_book(a, b):
    """True if two titles share their main words (protects against a wrong Gutenberg number)."""
    stop = {"the", "a", "an", "of", "in", "to", "and", "or", "at", "on", "under", "into", "being"}
    wa = set(re.findall(r"[a-z]+", (a or "").lower())) - stop
    wb = set(re.findall(r"[a-z]+", (b or "").lower())) - stop
    return bool(wa & wb)

KAIKKI_URLS = [
    "https://kaikki.org/dictionary/English/kaikki.org-dictionary-English.jsonl",
    "https://kaikki.org/dictionary/English/kaikki.org-dictionary-English.jsonl.gz",
]

WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")
SKIP_POS = {"name", "character", "symbol", "punct", "prefix", "suffix",
            "infix", "interfix", "affix", "circumfix", "phrase", "proverb"}
POS_SHORT = {"noun": "n.", "verb": "v.", "adj": "adj.", "adv": "adv.",
             "pron": "pron.", "prep": "prep.", "conj": "conj.", "intj": "interj.",
             "det": "det.", "num": "num.", "article": "art.", "particle": "part.",
             "contraction": "contr."}


def log(*a):
    print(*a, flush=True)


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa
            log(f"  fail {url}: {e}")
            if tries > 1:
                time.sleep(3 * (i + 1))
    return None


def book_urls(gid):
    """Gutenberg itself often refuses cloud servers, so try its mirrors too."""
    d = str(gid)
    sub = "0/" + d if len(d) == 1 else "/".join(d[:-1]) + "/" + d
    mirrors = ["https://gutenberg.pglaf.org", "https://aleph.pglaf.org",
               "https://mirrors.xmission.com/gutenberg", "https://gutenberg.readingroo.ms",
               "https://mirror.csclub.uwaterloo.ca/gutenberg"]
    urls = [f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt"]
    for m in mirrors:
        urls += [f"{m}/cache/epub/{gid}/pg{gid}.txt", f"{m}/{sub}/{gid}-0.txt", f"{m}/{sub}/{gid}.txt"]
    urls += [f"https://www.gutenberg.org/files/{gid}/{gid}-0.txt", f"https://www.gutenberg.org/files/{gid}/{gid}.txt"]
    return urls


GOOD_SOURCES = []


def fetch_book(gid):
    urls = book_urls(gid)
    # try the mirror that worked last time first
    urls.sort(key=lambda u: 0 if any(u.startswith(g) for g in GOOD_SOURCES) else 1)
    for u in urls:
        raw = fetch(u, tries=1)
        if raw and len(raw) > 2000 and b"<html" not in raw[:500].lower():
            base = u.split("/cache/")[0] if "/cache/" in u else u.rsplit("/", 1)[0].rsplit("/" + str(gid), 1)[0]
            base = re.sub(r"(/\d)+$", "", base)
            if base not in GOOD_SOURCES:
                GOOD_SOURCES.insert(0, base)
            log(f"  {gid} <- {u}")
            return raw
    return None


NUM_WORDS = ("One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|Eleven|Twelve|Thirteen|Fourteen|Fifteen|"
             "Sixteen|Seventeen|Eighteen|Nineteen|Twenty|First|Second|Third|Fourth|Fifth|Sixth|Seventh|"
             "Eighth|Ninth|Tenth|Last|the Last|the First")
HEAD_RE = re.compile(
    r"^(?:(?:CHAPTER|Chapter|STAVE|Stave|BOOK|Book|PART|Part|LETTER|Letter|ACT|Act|VOLUME|Volume|ADVENTURE|Adventure|STORY|Story|EPISODE|Episode)\s+"
    r"(?:[IVXLCDM]+|\d+|" + NUM_WORDS + r"|" + NUM_WORDS.upper() + r")\b(?:[.:\-—]?\s*.*)?"
    r"|(?:FIRST|SECOND|THIRD|FOURTH|FIFTH) ACT\b.*"
    r"|[IVXLC]{1,7}(?:\.\s*[A-Z0-9 ,'’\-—.:;!?]*)?"
    r"|[IVXLC]{1,7}\.\s+[A-Z][A-Za-z'’,\- ]{1,60}"         # I. The Blue Cross
    r"|\d{1,3}\.?(?:\s+[A-Z][A-Za-z'’,\- ]{1,60})?)$")      # 1  /  1. A Fellow Traveller


def is_heading(t):
    return len(t) <= 90 and bool(HEAD_RE.match(t))


def clean_book(raw):
    """Returns title, author and lines in the app's format:
    '#\t<text>' chapter heading, '~\t<text>' short line (contents, poems, letters), else a paragraph."""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    head = text[:6000]
    title = re.search(r"^Title:\s*(.+)$", head, re.M)
    author = re.search(r"^Author:\s*(.+)$", head, re.M)
    s = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", text, re.I | re.S)
    e = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", text, re.I)
    body = text[s.end() if s else 0: e.start() if e else len(text)]
    body = re.sub(r"\[Illustration[^\]]*\]", "", body, flags=re.S)
    body = body.replace("_", "")
    items = []  # (kind, text)  kind: p=paragraph, h=heading candidate, v=short line
    for block in re.split(r"\n\s*\n", body):
        lines = [re.sub(r"\s+", " ", l).strip() for l in block.split("\n")]
        lines = [l for l in lines if l and not re.fullmatch(r"[*\s.]+", l)]
        if not lines:
            continue
        heads = sum(1 for l in lines if is_heading(l))
        lens = [len(l) for l in lines]
        if len(lines) >= 2 and heads >= 2:                       # table of contents
            items += [("v", l) for l in lines]
        elif len(lines) >= 3 and sum(lens) / len(lens) < 48 and max(lens) < 70:   # poem / list
            items += [("v", l) for l in lines]
        elif len(lines) == 2 and is_heading(lines[0]) and len(lines[1]) <= 70:  # "Chapter I" / "The Cyclone"
            items.append(("h", lines[0].rstrip(".") + " — " + lines[1]))
        else:
            t = " ".join(lines)
            if is_heading(t):
                items.append(("h", t))
            elif len(t) < 60 and not re.search(r"[.!?\"”’,;:)\]]$", t):     # titles, headings
                items.append(("v", t))
            else:
                items.append(("p", t))
    out = []
    i = 0
    while i < len(items):
        kind, t = items[i]
        if kind == "h":
            # a real chapter has prose soon after it; otherwise it's a contents entry
            real = any(k == "p" and len(x) > 200 for k, x in items[i + 1:i + 5])
            if real:
                if i + 1 < len(items):
                    k2, t2 = items[i + 1]
                    if k2 != "h" and len(t2) <= 70 and (t2.isupper() or not re.search(r"[.!?\"”]$", t2)):
                        t = t.rstrip(".") + " — " + t2
                        i += 1
                out.append("#\t" + t)
            else:
                out.append("~\t" + t)
        elif kind == "v":
            out.append("~\t" + t)
        else:
            out.append(t)
        i += 1
    return (title.group(1).strip() if title else None,
            author.group(1).strip() if author else None,
            out)


def build_books():
    bdir = os.path.join(CACHE, "books")
    rdir = os.path.join(CACHE, "raw")
    os.makedirs(bdir, exist_ok=True)
    os.makedirs(rdir, exist_ok=True)
    meta = []
    for gid, level in BOOKS:
        rp = os.path.join(rdir, f"{gid}.txt")
        if os.path.exists(rp):
            raw = open(rp, "rb").read()
        else:
            raw = fetch_book(gid)
            if not raw:
                log(f"!! failed book {gid}")
                continue
            open(rp, "wb").write(raw)
            time.sleep(1)
        title, author, lines = clean_book(raw)
        words = sum(len(WORD_RE.findall(p)) for p in lines)
        chapters = sum(1 for l in lines if l.startswith("#\t"))
        with open(os.path.join(bdir, f"{gid}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        h = hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()[:8]   # new text -> new address, so phones never keep an old copy
        short, he, desc, year = INFO.get(gid, (title or f"Book {gid}", "", "", None))
        if title and not same_book(short, title):
            log(f"!! book {gid}: expected '{short}' but Gutenberg has '{title}' - showing the real title")
            short, he, desc, year = title, "", "", None
        meta.append({"id": gid, "title": short, "he": he, "desc": desc, "year": year, "author": author or "",
                     "genre": GENRE.get(gid, "classic"),
                     "level": level, "words": words, "chapters": chapters, "file": f"books/{gid}.txt?v={h}"})
        log(f"book {gid}: {title} / {author} ({words} words, {chapters} chapters)")
    os.makedirs(os.path.join(CACHE, "data"), exist_ok=True)
    json.dump(meta, open(os.path.join(CACHE, "data", "books.json"), "w", encoding="utf-8"), ensure_ascii=False)
    return meta


def book_vocab():
    vocab = set()
    bdir = os.path.join(CACHE, "books")
    for name in os.listdir(bdir):
        for w in WORD_RE.findall(open(os.path.join(bdir, name), encoding="utf-8").read().replace("#\t", " ").replace("~\t", " ")):
            w = w.lower().replace("’", "'")
            vocab.add(w)
            if w.endswith("'s"):
                vocab.add(w[:-2])
    return vocab


def open_stream(url):
    req = urllib.request.Request(url, headers=UA)
    r = urllib.request.urlopen(req, timeout=120)
    if url.endswith(".gz"):
        return io.TextIOWrapper(gzip.GzipFile(fileobj=r), encoding="utf-8")
    return io.TextIOWrapper(r, encoding="utf-8")


def he_translations(entry):
    out = []
    lists = [entry.get("translations") or []]
    for s in entry.get("senses") or []:
        lists.append(s.get("translations") or [])
    for lst in lists:
        for t in lst:
            code = t.get("lang_code") or t.get("code")
            if code == "he" or t.get("lang") == "Hebrew":
                w = (t.get("word") or "").strip()
                if w and w not in out and w != "-":
                    out.append(w)
    return out


def build_dict(vocab):
    path = os.path.join(CACHE, "data", "dict.json")
    sig = hashlib.md5(" ".join(sorted(vocab)).encode()).hexdigest()[:12]
    if os.path.exists(path):
        try:
            if json.load(open(path, encoding="utf-8")).get("sig") == sig:
                log("dict: using cache")
                return
        except Exception:  # noqa
            pass
        log("dict: book list changed, rebuilding")
        # keep earlier machine translations so they are not computed again
        try:
            old = json.load(open(path, encoding="utf-8"))["l"]
            memo_path = os.path.join(CACHE, "data", "mt_memo.json")
            memo = json.load(open(memo_path, encoding="utf-8")) if os.path.exists(memo_path) else {}
            for w, v in old.items():
                if len(v) > 2 and v[2] == 1 and v[0]:
                    memo.setdefault(w, v[0])
            json.dump(memo, open(memo_path, "w", encoding="utf-8"), ensure_ascii=False)
            log(f"dict: kept {len(memo)} earlier machine translations")
        except Exception as e:  # noqa
            log("dict: could not keep old translations:", e)
    lemmas = {}                  # word -> {"he": [], "d": []}
    forms = defaultdict(list)    # form -> [lemma]
    n = 0
    stream = None
    for url in KAIKKI_URLS:
        try:
            stream = open_stream(url)
            log("dict source:", url)
            break
        except Exception as e:  # noqa
            log("dict source failed:", url, e)
    if stream is None:
        log("!! no dictionary source, writing empty dict (app will use online fallback)")
        json.dump({"l": {}, "f": {}}, open(path, "w", encoding="utf-8"))
        return
    for line in stream:
        n += 1
        if n % 200000 == 0:
            log(f"  {n} entries, {len(lemmas)} kept")
        try:
            e = json.loads(line)
        except Exception:  # noqa
            continue
        word = e.get("word") or ""
        pos = e.get("pos") or ""
        if " " in word or pos in SKIP_POS or e.get("lang_code", "en") != "en":
            continue
        low = word.lower()
        senses = e.get("senses") or []
        # inflected forms listed on the lemma ("walk" -> walked, walking)
        entry_forms = []
        for f in e.get("forms") or []:
            fw = (f.get("form") or "").lower()
            tags = f.get("tags") or []
            if fw and fw != low and fw in vocab and "table-tags" not in tags and "inflection-template" not in tags:
                entry_forms.append(fw)
        # "walked": form of "walk"
        form_of = []
        real_glosses = []
        for s in senses:
            fo = s.get("form_of") or s.get("alt_of")
            if fo:
                for x in fo:
                    if x.get("word"):
                        form_of.append(x["word"].lower())
                continue
            g = s.get("glosses") or s.get("raw_glosses")
            if g and "no-gloss" not in (s.get("tags") or []):
                real_glosses.append(g[-1])
        if low in vocab:
            for lem in form_of:
                if lem != low and lem not in forms[low]:
                    forms[low].append(lem)
        for fw in entry_forms:
            if low not in forms[fw]:
                forms[fw].append(low)
        if not (low in vocab or entry_forms):
            continue
        he = he_translations(e)
        if not real_glosses and not he:
            continue
        d = lemmas.setdefault(low, {"he": [], "d": []})
        for h in he:
            if h not in d["he"] and len(d["he"]) < 5:
                d["he"].append(h)
        if real_glosses and len(d["d"]) < 3:
            g = re.sub(r"\s+", " ", real_glosses[0]).strip()
            if len(g) > 150:
                g = g[:147].rsplit(" ", 1)[0] + "…"
            d["d"].append(f"({POS_SHORT.get(pos, pos)}) {g}")
    log(f"dict: scanned {n} entries")
    out_l = {}
    for w, d in lemmas.items():
        out_l[w] = [", ".join(d["he"]), " · ".join(d["d"])]
    out_f = {}
    for fw, lems in forms.items():
        if fw not in vocab:
            continue
        good = [x for x in lems if x in out_l]
        if good:
            # prefer a lemma that has Hebrew
            good.sort(key=lambda x: 0 if out_l[x][0] else 1)
            out_f[fw] = good[0]
    # drop lemmas that are neither in the books nor reachable through a form
    reach = set(out_f.values())
    out_l = {w: v for w, v in out_l.items() if w in vocab or w in reach}
    with_he = sum(1 for v in out_l.values() if v[0])
    log(f"dict: {len(out_l)} words ({with_he} with Hebrew), {len(out_f)} forms")
    cov = sum(1 for w in vocab if w in out_l or w in out_f)
    log(f"dict: covers {cov}/{len(vocab)} distinct book words")
    json.dump({"l": out_l, "f": out_f, "sig": sig}, open(path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))


def assemble():
    if os.path.exists(SITE):
        shutil.rmtree(SITE)
    shutil.copytree(os.path.join(ROOT, "app"), SITE)
    shutil.copytree(os.path.join(CACHE, "books"), os.path.join(SITE, "books"))
    os.makedirs(os.path.join(SITE, "data"))
    for f in ("books.json", "dict.json"):
        shutil.copy(os.path.join(CACHE, "data", f), os.path.join(SITE, "data", f))
    version = os.environ.get("GITHUB_SHA", str(int(time.time())))[:10]
    sw = os.path.join(SITE, "sw.js")
    s = open(sw, encoding="utf-8").read().replace("__BUILD__", version)
    open(sw, "w", encoding="utf-8").write(s)
    open(os.path.join(SITE, ".nojekyll"), "w").close()
    log("site assembled, version", version)


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else "all"
    if step in ("data", "all"):
        os.makedirs(os.path.join(CACHE, "data"), exist_ok=True)
        meta = build_books()
        if not meta:
            sys.exit("no books downloaded")
        build_dict(book_vocab())
    if step in ("site", "all"):
        assemble()
