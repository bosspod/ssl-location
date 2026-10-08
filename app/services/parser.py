import re

from app.schemas import InputType, ParsedLocation
from app.services.normalizer import LocationNormalizer
from app.utils import normalize_phone


class LocationParserService:
    PROVINCES = (
        "กรุงเทพมหานคร",
        "กระบี่",
        "กาญจนบุรี",
        "กาฬสินธุ์",
        "กำแพงเพชร",
        "ขอนแก่น",
        "จันทบุรี",
        "ฉะเชิงเทรา",
        "ชลบุรี",
        "ชัยนาท",
        "ชัยภูมิ",
        "ชุมพร",
        "เชียงราย",
        "เชียงใหม่",
        "ตรัง",
        "ตราด",
        "ตาก",
        "นครนายก",
        "นครปฐม",
        "นครพนม",
        "นครราชสีมา",
        "นครศรีธรรมราช",
        "นครสวรรค์",
        "นนทบุรี",
        "นราธิวาส",
        "น่าน",
        "บึงกาฬ",
        "บุรีรัมย์",
        "ปทุมธานี",
        "ประจวบคีรีขันธ์",
        "ปราจีนบุรี",
        "ปัตตานี",
        "พระนครศรีอยุธยา",
        "พะเยา",
        "พังงา",
        "พัทลุง",
        "พิจิตร",
        "พิษณุโลก",
        "เพชรบุรี",
        "เพชรบูรณ์",
        "แพร่",
        "ภูเก็ต",
        "มหาสารคาม",
        "มุกดาหาร",
        "แม่ฮ่องสอน",
        "ยโสธร",
        "ยะลา",
        "ร้อยเอ็ด",
        "ระนอง",
        "ระยอง",
        "ราชบุรี",
        "ลพบุรี",
        "ลำปาง",
        "ลำพูน",
        "เลย",
        "ศรีสะเกษ",
        "สกลนคร",
        "สงขลา",
        "สตูล",
        "สมุทรปราการ",
        "สมุทรสงคราม",
        "สมุทรสาคร",
        "สระแก้ว",
        "สระบุรี",
        "สิงห์บุรี",
        "สุโขทัย",
        "สุพรรณบุรี",
        "สุราษฎร์ธานี",
        "สุรินทร์",
        "หนองคาย",
        "หนองบัวลำภู",
        "อ่างทอง",
        "อำนาจเจริญ",
        "อุดรธานี",
        "อุตรดิตถ์",
        "อุทัยธานี",
        "อุบลราชธานี",
    )
    ADDRESS_MARKERS = (
        "บ้านเลขที่",
        "หมู่บ้าน",
        "หมู่",
        "อาคาร",
        "ซอย",
        "ถนน",
        "แขวง",
        "ตำบล",
        "เขต",
        "อำเภอ",
        "จังหวัด",
    )

    def __init__(self, normalizer: LocationNormalizer | None = None) -> None:
        self.normalizer = normalizer or LocationNormalizer()

    @staticmethod
    def _extract(pattern: str, text: str) -> str | None:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return match.group(1).strip(" ,") if match else None

    @staticmethod
    def _deduplicate_latin_phrases(value: str | None) -> str | None:
        if not value:
            return value
        tokens = value.split()
        output: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        index = 0
        while index < len(tokens):
            if index + 1 < len(tokens) and all(
                re.fullmatch(r"[A-Za-zÀ-ÿ]+", token) for token in tokens[index : index + 2]
            ):
                pair = (tokens[index].casefold(), tokens[index + 1].casefold())
                if pair in seen_pairs:
                    index += 2
                    continue
                seen_pairs.add(pair)
            output.append(tokens[index])
            index += 1
        return " ".join(output) or None

    def parse(self, raw: str) -> ParsedLocation:
        text = self.normalizer.normalize(raw)
        phone_match = re.search(r"(?<!\d)(0\d{8,9})(?!\d)", text)
        phone = normalize_phone(phone_match.group(1)) if phone_match else None
        postal = self._extract(r"(?<!\d)([1-9]\d{4})(?!\d)", text)
        house = self._extract(r"(?:^|บ้านเลขที่\s*)(\d+(?:[/-]\d+)*)(?=\s|$)", text)
        if not house:
            # Fractions inside a soi/road name are not house numbers.
            prefix = re.split(r"หมู่บ้าน|ซอย|ถนน", text, maxsplit=1)[0]
            house = self._extract(r"(?<!\d)(\d+[/-]\d+)(?=\s|$)", prefix)
            if not house and prefix != text:
                house = self._extract(r"(?:^|\s)(\d{1,4})\s*$", prefix)
        if house in {phone, postal}:
            house = None
        moo = self._extract(r"(?:หมู่(?:ที่)?\s*)(\d+)", text)
        village = self._extract(
            r"หมู่บ้าน\s*([^,]+?)(?=\s+(?:ซอย|ถนน|แขวง|ตำบล|เขต|อำเภอ|จังหวัด)|\s+[1-9]\d{4}|$)", text
        )
        building = self._extract(
            r"อาคาร\s*([^,]+?)(?=\s+(?:ซอย|ถนน|แขวง|ตำบล|เขต|อำเภอ|จังหวัด)|$)", text
        )
        soi = self._extract(
            r"ซอย\s*([^,]+?)(?=\s+(?:ถนน|แขวง|ตำบล|เขต|อำเภอ|จังหวัด)|\s+[1-9]\d{4}|$)", text
        )
        road = self._extract(
            r"ถนน\s*([^,]+?)(?=\s+(?:แขวง|ตำบล|เขต|อำเภอ|จังหวัด)|\s+[1-9]\d{4}|$)", text
        )
        subdistrict = self._extract(
            r"(?:แขวง|ตำบล)\s*([^,]+?)(?=\s+(?:แขวง|ตำบล|เขต|อำเภอ|จังหวัด)"
            r"|\s+\d+(?:[/-]\d+)+|\s+[1-9]\d{4}|$)",
            text,
        )
        district = self._extract(
            r"(?:เขต|อำเภอ)\s*([^,]+?)(?=\s+(?:จังหวัด|กรุงเทพมหานคร)|\s+[1-9]\d{4}|$)", text
        )
        province = next(
            (
                item
                for item in sorted(self.PROVINCES, key=len, reverse=True)
                if re.search(rf"(?:จังหวัด\s*|^|[\s,]){re.escape(item)}(?=$|[\s,]|\d)", text)
            ),
            None,
        )

        customer = self._extract(
            r"(?:ของ)?ลูกค้า\s*([^,]+?)(?=\s+(?:ใกล้|ตรงข้าม|ข้าง|ติด|จังหวัด)|$)", text
        )
        landmark = self._extract(
            r"(?:ใกล้|ตรงข้าม|ข้าง|ติด(?:กับ)?)\s*([^,]+?)(?=\s+(?:แขวง|ตำบล|เขต|อำเภอ|จังหวัด)|$)",
            text,
        )

        address_positions = [text.find(marker) for marker in self.ADDRESS_MARKERS if marker in text]
        positions = list(address_positions)
        if province and province in text:
            positions.append(text.find(province))
        if customer:
            customer_marker = text.find("ลูกค้า")
            if customer_marker >= 0:
                positions.append(customer_marker)
        prefix = text[: min(positions)] if positions else text
        if house and prefix.startswith(house):
            prefix = prefix[len(house) :]
        prefix = re.sub(r"(?<!\d)[1-9]\d{4}(?!\d)|0\d{8,9}", " ", prefix)
        place_name = re.sub(r"\s+", " ", prefix).strip(" ,") or None
        place_name = self._deduplicate_latin_phrases(place_name)
        if place_name and re.fullmatch(r"(?:ส่ง)?ที่เดิม(?:ของ)?", place_name):
            place_name = None

        has_address = bool(address_positions or house or postal or (province and not place_name))
        count = sum(
            bool(item)
            for item in (house, village, soi, road, subdistrict, district, province, postal)
        )
        if phone and not place_name and not has_address:
            input_type = InputType.PHONE
        elif phone and place_name:
            input_type = InputType.PLACE_WITH_PHONE
        elif count >= 5:
            input_type = InputType.FULL_ADDRESS
        elif has_address:
            input_type = InputType.ADDRESS if count >= 3 else InputType.PARTIAL_ADDRESS
        elif place_name:
            input_type = InputType.PLACE
        else:
            input_type = InputType.UNKNOWN

        confidence = {
            key: 1.0 if key in {"phone", "postal_code", "province"} else 0.9
            for key, value in {
                "phone": phone,
                "postal_code": postal,
                "province": province,
                "district": district,
                "subdistrict": subdistrict,
            }.items()
            if value
        }
        return ParsedLocation(
            raw_input=raw,
            normalized_input=text,
            place_name=place_name,
            company_name=place_name
            if place_name and place_name.lower().startswith("บริษัท")
            else None,
            customer_name=customer,
            phone=phone,
            house_number=house,
            moo=moo,
            village=village,
            building=building,
            soi=soi,
            road=road,
            subdistrict=subdistrict,
            district=district,
            province=province,
            postal_code=postal,
            landmark=landmark,
            input_type=input_type,
            entity_confidence=confidence,
        )
