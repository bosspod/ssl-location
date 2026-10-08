from app.schemas import ParsedLocation


class SearchQueryGenerator:
    def __init__(self, maximum: int = 5) -> None:
        self.maximum = maximum

    def generate(self, parsed: ParsedLocation) -> list[str]:
        candidates: list[str] = [parsed.normalized_input]
        place = parsed.place_name
        region = " ".join(
            filter(
                None,
                [
                    parsed.area_context,
                    parsed.subdistrict,
                    parsed.district,
                    parsed.province,
                ],
            )
        )
        if place:
            candidates.extend(
                [
                    " ".join(filter(None, [place, parsed.phone, parsed.province])),
                    " ".join(filter(None, [place, parsed.district, parsed.province])),
                    " ".join(filter(None, [place, region])),
                    place,
                ]
            )
        for alias in parsed.aliases:
            candidates.extend([" ".join(filter(None, [alias, region])), alias])
        if parsed.company_name and parsed.company_name != place:
            candidates.extend(
                [
                    parsed.company_name,
                    " ".join(filter(None, [parsed.company_name, parsed.area_context])),
                    " ".join(filter(None, [parsed.company_name, parsed.province])),
                    " ".join(filter(None, [parsed.company_name, parsed.branch_name, region])),
                ]
            )
        if parsed.phone:
            candidates.append(parsed.phone)
        address_parts = [
            parsed.house_number,
            f"หมู่บ้าน{parsed.village}" if parsed.village else None,
            f"ซอย{parsed.soi}" if parsed.soi else None,
            f"ถนน{parsed.road}" if parsed.road else None,
            parsed.subdistrict,
            parsed.district,
            parsed.province,
            parsed.postal_code,
        ]
        full = " ".join(filter(None, address_parts))
        if full:
            candidates.append(full)
        without_house = " ".join(filter(None, address_parts[1:]))
        if without_house and without_house != full:
            candidates.append(without_house)
        if parsed.village:
            candidates.append(" ".join(filter(None, [f"หมู่บ้าน{parsed.village}", region])))
            if parsed.soi:
                candidates.append(
                    " ".join(
                        filter(
                            None,
                            [
                                f"หมู่บ้าน{parsed.village}",
                                f"ซอย{parsed.soi}",
                                parsed.province,
                            ],
                        )
                    )
                )
        if parsed.soi:
            candidates.append(
                " ".join(filter(None, [f"ซอย{parsed.soi}", parsed.road, parsed.province]))
            )
        if parsed.road:
            candidates.append(" ".join(filter(None, [f"ถนน{parsed.road}", region])))
        if parsed.building:
            candidates.append(" ".join(filter(None, [parsed.building, region])))
        operational_terms = ("warehouse", "plant", "site", "branch", "depot", "โกดัง", "คลัง")
        if parsed.company_name or parsed.site_name or any(
            term in (place or "").casefold() for term in operational_terms
        ):
            base = parsed.company_name or parsed.site_name or place
            candidates.extend(
                " ".join(filter(None, [base, term, parsed.district or parsed.province]))
                for term in operational_terms
            )
        if parsed.landmark:
            candidates.append(" ".join(filter(None, [parsed.landmark, region])))
        if parsed.customer_name:
            candidates.append(" ".join(filter(None, [parsed.customer_name, region])))
        if parsed.district and parsed.province:
            candidates.append(f"{parsed.district} {parsed.province}")
        return list(dict.fromkeys(query.strip() for query in candidates if query.strip()))[
            : self.maximum
        ]
