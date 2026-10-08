import re
import unicodedata

from app.utils import normalize_phone


class LocationNormalizer:
    _abbreviations = (
        (r"(?<!\S)กทม\.?($|\s)", "กรุงเทพมหานคร "),
        (r"(?<!\S)จ\.(?=\s*[^\s])", "จังหวัด"),
        (r"(?<!\S)อ\.(?=\s*[^\s])", "อำเภอ"),
        (r"(?<!\S)ต\.(?![A-Za-zก-๙]\.)(?=\s*[^\s])", "ตำบล"),
        (r"(?<!\S)ถ\.(?=\s*[^\s])", "ถนน"),
        (r"(?<!\S)ซ\.(?=\s*[^\s])", "ซอย"),
        (r"(?<!\S)ม\.(?=\s*\d)", "หมู่ "),
    )

    def normalize(self, value: str) -> str:
        text = unicodedata.normalize("NFC", value).strip()
        text = text.translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
        text = text.translate(str.maketrans("，、；;,", "     "))
        text = re.sub(
            r"(?:โทร(?:ศัพท์)?|(?<![A-Za-z])(?:tel(?:ephone)?|phone))"
            r"\s*[:：]?\s*(?=\+?\d)",
            " ", text, flags=re.IGNORECASE,
        )
        text = re.sub(r"(\+?66)\s*\(0\)", r"\1", text)
        text = re.sub(r"(\d)\s*/\s*(?=\d)", r"\1/", text)
        text = re.sub(r"(?<=[ก-๙0-9])(?=(?:ต\.|อ\.|จ\.|ถ\.|ซ\.))", " ", text)
        text = re.sub(r"[\t\r\n]+", " ", text)
        for pattern, replacement in self._abbreviations:
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        text = re.sub(
            r"(?<!\d)(?:\+?66[\s-]?|0)(?:\d[\s-]?){8,9}(?!\d)",
            lambda match: normalize_phone(match.group(0)) or match.group(0),
            text,
        )
        return re.sub(r"\s+", " ", text).strip(" ,")
