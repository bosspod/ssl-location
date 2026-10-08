import math
import re
import unicodedata

from rapidfuzz.fuzz import ratio


def compact_text(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFC", value).casefold()
    return re.sub(r"[^\w\u0E00-\u0E7F]+", "", value)


def string_similarity(left: str | None, right: str | None) -> float:
    a, b = compact_text(left), compact_text(right)
    return ratio(a, b) / 100 if a and b else 0.0


def normalize_place_name(value: str | None) -> str:
    normalized = compact_text(value)
    for prefix in ("ร้าน", "บริษัท", "ห้างหุ้นส่วนจำกัด"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    return normalized


def place_name_similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_place_name(left), normalize_place_name(right)
    return ratio(a, b) / 100 if a and b else 0.0


def normalize_admin_area(value: str | None) -> str:
    normalized = compact_text(value)
    aliases = {
        "bangkok": "กรุงเทพมหานคร",
        "bangkokmetropolis": "กรุงเทพมหานคร",
        "krungthepmahanakhon": "กรุงเทพมหานคร",
        "กรุงเทพ": "กรุงเทพมหานคร",
    }
    normalized = aliases.get(normalized, normalized)
    for prefix in ("จังหวัด", "อำเภอ", "เขต", "ตำบล", "แขวง"):
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            return normalized[len(prefix) :]
    return normalized


def admin_area_similarity(left: str | None, right: str | None) -> float:
    a, b = normalize_admin_area(left), normalize_admin_area(right)
    return ratio(a, b) / 100 if a and b else 0.0


def calculate_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("66") and len(digits) in {11, 12}:
        digits = "0" + digits[2:]
    return digits if 9 <= len(digits) <= 10 else None


def mask_phones(value: str | None) -> str | None:
    if not value:
        return value
    return re.sub(r"(?<!\d)(0\d{2})\d{4}(\d{3})(?!\d)", r"\1****\2", value)
