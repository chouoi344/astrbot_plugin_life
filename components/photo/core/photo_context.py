"""Two recent successful photo groups, isolated by chat and persona."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

SHOT_FIELDS = ("capture", "framing", "angle", "action", "expression")
_CLAUSES = re.compile(r"[，,。.!！?？；;\n]")
_NON_PHOTO_NOUN = r"(?:日程表|图表|表格|表(?!情)|纸|卡片|卡|票据|票)"
_NON_PHOTO_ORDINAL_PREFIX = re.compile(rf"{_NON_PHOTO_NOUN}(?:里|中)?的?\s*$")
_PHOTO_NOUN = re.compile(r"\s*(?:照片|图片|自拍|成片|图(?!表))")
_GROUP_INDEX = re.compile(
    rf"第\s*(\d+|[零〇一二两三四五六七八九十百千万]+)\s*"
    rf"(?:(张(?!\s*{_NON_PHOTO_NOUN})|照片|图(?!表))|(?=$|看|换|改|调|做))"
)
_INDEX_NUMBER = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}
_PREVIOUS_GROUP = r"上一组|前一组"
_LATEST_MULTI_GROUP = r"刚才(?:的)?[这那](?:一)?组|这(?:一)?组|最近(?:的)?一组"
_GROUP_QUALIFIER = re.compile(rf"(?P<previous>{_PREVIOUS_GROUP})|(?P<latest_multi>{_LATEST_MULTI_GROUP})")
_HISTORICAL_PHOTO = re.compile(
    rf"上(?:一)?张|{_PREVIOUS_GROUP}|刚(?:才|刚)(?:[这那][张组]|拍的|发(?:给我)?的|的(?:照片|图)|(?=\s*第))|刚拍的(?:那张|照片)|前一张|旧图"
)
_PREVIOUS_PHOTO = re.compile(_HISTORICAL_PHOTO.pattern + r"|这(?:一)?组")
_SINGULAR_OLD_PHOTO = re.compile(
    rf"(?:上(?:一)?|前一|刚(?:才|刚)(?:的|拍的|发(?:给我)?的)?[这那](?:一)?|刚拍的那)张(?!\s*{_NON_PHOTO_NOUN})"
    r"|刚才(?:拍|发)?的(?:那张)?(?:照片|图(?!表))|旧照片|旧图"
)
_NEGATED_OLD_VIEW = re.compile(r"(?:不要|别|不用|不必|无需|不需要|不)(?:再)?(?:看|参考)")
_PHOTO_TARGET = re.compile(rf"[这那](?:一)?张(?!\s*{_NON_PHOTO_NOUN})")
_OBJECT_EDIT = re.compile(r"(?:把|将)[这那](?![^，,。；;\n]*日程)[^，,。；;\n]{1,20}?(?:改成|调整)")
_IMAGE_LIMIT = re.compile(r"其他别变|图片|画幅")
_ATTACHMENT_ONLY = re.compile(
    r"(?:只|仅)(?:用|使用|参考|看)[^，,。；;\n]{0,12}(?:附件|上传的(?:照片|图)|本轮(?:照片|图片|图)|这次发的(?:照片|图))"
)
_NEGATED_CLAUSE = re.compile(
    r"^(?:这次|请)?(?:不要|别|不用|不必)(?:再)?(?:用|使用|参考|看|换|改|修|调|编辑|拍|来|重发|发)"
    rf"|^(?:这次|请)?(?:不是|不要|别|不用|不选|排除)(?:再)?(?:选)?(?:{_GROUP_QUALIFIER.pattern})"
)
_FRESH_PHOTO = re.compile(r"(?:拍|来)(?:一)?张\s*(?:全新|新(?:的)?(?:照片|图片|自拍|图))")
_CONTINUE_PHOTO = re.compile(
    r"再(?:帮我|给我)?(?:拍|来)(?:一)?张"
    r"|续拍|换(?:个|一个|一下)?(?:拍摄)?(?:角度|机位)"
    r"(?=$|(?:再|给我)?拍|(?:看看|看一下|行吗|可以吗|吗|吧)$)"
)
_EDIT_PHOTO = re.compile(
    r"改图|修图|修改|编辑|改(?:一下|成)|换(?:个|一下)?背景|背景(?:换|改)|"
    r"调整|去掉|加上|做(?:点|些)?变化"
)
_BEFORE_NEGATED_EDIT = re.compile(r"(?:不|不要|别|无需|不用|不必)(?:再|做(?:任何)?)?$")
_RESEND_PHOTO = re.compile(
    r"^(?:请|麻烦你?|能不能|可以)?(?:重发|重新发|再发|发(?:给)?我|给我发)(?!的)"
    r"|(?:发(?:给)?我|给我发|发一下|发过来|原图给我)(?:一下|一遍)?(?:吧|呀|哈)?$"
)


def _photo_ordinals(clause: str):
    """Ignore document ordinals unless the ordinal names a photo explicitly."""
    for match in _GROUP_INDEX.finditer(clause):
        if (
            not _NON_PHOTO_ORDINAL_PREFIX.search(clause[: match.start()])
            or match.group(2) in {"照片", "图"}
            or _PHOTO_NOUN.match(clause, match.end())
        ):
            yield match


@dataclass(frozen=True)
class PhotoScope:
    umo: str
    conversation_id: str
    persona_id: str


@dataclass(frozen=True)
class PhotoRecord:
    scope: PhotoScope
    photo_id: str
    path: Path
    group_id: str
    index: int
    photo_type: str
    mode: str
    description: str
    state: dict
    shot: dict
    reference_images: tuple[bytes, ...] = ()
    reference_roles: tuple[str, ...] = ()


class PhotoContextStore:
    def __init__(self, path: Path, cache_dir: Path):
        self.path = path
        self.cache_dir = cache_dir.resolve()
        self._groups: dict[PhotoScope, list[list[PhotoRecord]]] = {}
        self._selected: dict[PhotoScope, str] = {}
        if path.exists():
            self._load(json.loads(path.read_text(encoding="utf-8")))

    def _load(self, data):
        if not isinstance(data, list):
            raise ValueError("照片上下文格式无效")
        for group in data:
            if not isinstance(group, dict):
                raise ValueError("照片上下文分组无效")
            scope_data = group.get("scope")
            if (
                not isinstance(scope_data, dict)
                or set(scope_data) != {"umo", "conversation_id", "persona_id"}
                or any(not isinstance(value, str) for value in scope_data.values())
            ):
                raise ValueError("照片上下文会话无效")
            scope = PhotoScope(**scope_data)
            selected = group.get("selected")
            if scope in self._groups or not isinstance(selected, str):
                raise ValueError("照片上下文分组无效")
            latest = self._load_records(scope, group.get("photos"))
            groups = [latest]
            if "previous_photos" in group:
                previous = self._load_records(scope, group["previous_photos"])
                if latest[0].group_id == previous[0].group_id or {record.photo_id for record in latest}.intersection(
                    record.photo_id for record in previous
                ):
                    raise ValueError("照片上下文分组无效")
                groups.append(previous)
            self._groups[scope] = groups
            self._selected[scope] = selected
        self._prune()

    def _load_records(self, scope, photos):
        if not isinstance(photos, list) or not 1 <= len(photos) <= 4:
            raise ValueError("照片上下文分组无效")
        records = []
        for row in photos:
            text_fields = (
                "photo_id",
                "filename",
                "group_id",
                "photo_type",
                "mode",
                "description",
            )
            if (
                not isinstance(row, dict)
                or any(not isinstance(row.get(key), str) for key in text_fields)
                or type(row.get("index")) is not int
                or not 1 <= row["index"] <= 4
                or not isinstance(row.get("state"), dict)
                or not isinstance(row.get("shot"), dict)
                or not isinstance(row.get("reference_roles"), list)
                or any(not isinstance(role, str) for role in row["reference_roles"])
                or set(row["shot"]) != set(SHOT_FIELDS)
                or any(not isinstance(value, str) for value in row["shot"].values())
            ):
                raise ValueError("照片上下文记录无效")
            filename = row["filename"]
            path = self.cache_dir / filename
            if (
                not filename
                or "/" in filename
                or "\\" in filename
                or filename in {".", ".."}
                or path.resolve().parent != self.cache_dir
            ):
                raise ValueError("照片上下文文件名无效")
            if records and (
                row["group_id"] != records[0].group_id
                or any(r.photo_id == row["photo_id"] or r.index == row["index"] for r in records)
            ):
                raise ValueError("照片上下文分组无效")
            records.append(
                PhotoRecord(
                    scope=scope,
                    photo_id=row["photo_id"],
                    path=path,
                    group_id=row["group_id"],
                    index=row["index"],
                    photo_type=row["photo_type"],
                    mode=row["mode"],
                    description=row["description"],
                    state=row["state"],
                    shot=row["shot"],
                    reference_roles=tuple(row["reference_roles"]),
                )
            )
        return records

    def _prune(self):
        available = {}
        for scope, groups in list(self._groups.items()):
            retained = []
            for records in groups:
                usable = []
                for index, record in enumerate(records):
                    if record.path.is_file() and record.path.resolve().parent == self.cache_dir:
                        usable.append(record)
                    elif record.reference_images:
                        records[index] = replace(record, reference_images=())
                retained.append(usable)
            if not any(retained):
                del self._groups[scope]
                del self._selected[scope]
                continue
            # Keep the original slots so an evicted target cannot change which
            # batch a group qualifier denotes.
            available[scope] = retained
            if not any(record.photo_id == self._selected[scope] for records in retained for record in records):
                self._selected[scope] = next(records for records in retained if records)[-1].photo_id
        return available

    def _save(self):
        def stored_records(records):
            return [
                {
                    "photo_id": record.photo_id,
                    "filename": record.path.name,
                    "group_id": record.group_id,
                    "index": record.index,
                    "photo_type": record.photo_type,
                    "mode": record.mode,
                    "description": record.description,
                    "state": record.state,
                    "shot": {key: record.shot[key] for key in SHOT_FIELDS},
                    "reference_roles": record.reference_roles,
                }
                for record in records
            ]

        data = []
        for scope, groups in self._groups.items():
            group = {
                "scope": asdict(scope),
                "selected": self._selected[scope],
                "photos": stored_records(groups[0]),
            }
            if len(groups) == 2:
                group["previous_photos"] = stored_records(groups[1])
            data.append(group)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def remember(self, record: PhotoRecord) -> None:
        """Register a confirmed successful send; callers never pass pending shots."""
        self._prune()
        if not record.path.is_file() or record.path.resolve().parent != self.cache_dir:
            return
        groups = self._groups.get(record.scope, [])
        records = groups[0] if groups else []
        if not records or records[0].group_id != record.group_id:
            if groups and (record.index != 1 or groups[-1][0].group_id == record.group_id):
                return
            groups = [[], *groups[:1]]
            records = []
        records = [r for r in records if r.index != record.index and r.photo_id != record.photo_id]
        records.append(copy.deepcopy(record))
        groups[0] = sorted(records, key=lambda photo: photo.index)
        self._groups[record.scope] = groups
        self._selected[record.scope] = record.photo_id
        self._save()

    def get(self, scope: PhotoScope, photo_id: str = "") -> PhotoRecord | None:
        available = self._prune().get(scope, ())
        selected = photo_id or self._selected.get(scope, "")
        return next(
            (copy.deepcopy(record) for records in available for record in records if record.photo_id == selected),
            None,
        )

    def explicit_group_reference(self, scope: PhotoScope, user_text: str) -> tuple[bool, PhotoRecord | None]:
        """Resolve a group-qualified ordinal, including an unavailable explicit target."""
        requested = [
            clause.strip() for clause in _CLAUSES.split(user_text) if not _NEGATED_CLAUSE.search(clause.strip())
        ]
        qualifiers = {match.lastgroup for clause in requested for match in _GROUP_QUALIFIER.finditer(clause)}
        indices = {_INDEX_NUMBER.get(match.group(1)) for clause in requested for match in _photo_ordinals(clause)}
        if not qualifiers or not indices:
            return False, None
        if len(qualifiers) != 1 or len(indices) != 1 or None in indices:
            return True, None
        available = self._prune().get(scope, ())
        if "previous" in qualifiers:
            records = available[1] if len(available) == 2 else ()
        else:
            records = next(
                (
                    usable
                    for group, usable in zip(self._groups.get(scope, ()), available)
                    if any(record.index > 1 for record in group)
                ),
                (),
            )
        return True, next(
            (copy.deepcopy(record) for record in records if record.index in indices),
            None,
        )

    def planning_reference(
        self, scope: PhotoScope, user_text: str, *, has_new_images: bool = False
    ) -> PhotoRecord | None:
        """Resolve a successful source photo for an explicit planning request."""
        if _ATTACHMENT_ONLY.search(user_text) or _FRESH_PHOTO.search(user_text):
            return None
        clauses = [clause.strip() for clause in _CLAUSES.split(user_text)]
        if any(
            _PREVIOUS_PHOTO.search(clause[match.end() :])
            for clause in clauses
            for match in _NEGATED_OLD_VIEW.finditer(clause)
        ):
            return None
        requested = [clause for clause in clauses if not _NEGATED_CLAUSE.search(clause)]
        matched_group, group_record = self.explicit_group_reference(scope, user_text)
        if has_new_images and not matched_group and not any(_HISTORICAL_PHOTO.search(clause) for clause in requested):
            return None
        mentions_photo = any(_PREVIOUS_PHOTO.search(clause) for clause in clauses)
        indices = {
            match.group(1)
            for clause in requested
            for match in _photo_ordinals(clause)
            if match.group(2) or mentions_photo
        }
        continues = any(_CONTINUE_PHOTO.search(clause) for clause in requested)
        edits = any(
            not _BEFORE_NEGATED_EDIT.search(clause[: match.start()])
            for clause in requested
            for match in _EDIT_PHOTO.finditer(clause)
        )
        photo_target = (
            matched_group
            or bool(indices)
            or any(_SINGULAR_OLD_PHOTO.search(clause) or _PHOTO_TARGET.search(clause) for clause in requested)
            or (
                not has_new_images
                and _IMAGE_LIMIT.search(user_text)
                and any(_OBJECT_EDIT.search(clause) for clause in requested)
            )
        )
        # A definite photo target is enough to give the model its visual context.
        if not continues and (
            not photo_target or (not edits and any(_RESEND_PHOTO.search(clause) for clause in requested))
        ):
            return None
        if matched_group:
            return group_record
        if indices:
            selected = {_INDEX_NUMBER.get(index) for index in indices}
            if len(selected) != 1 or None in selected:
                return None
            index = selected.pop()
            groups = self._prune().get(scope, ())
            if not groups:
                return None
            records = groups[0]
            if index > 1 and len(groups) == 2 and all(record.index == 1 for record in self._groups[scope][0]):
                records = groups[1]
            return next(
                (copy.deepcopy(record) for record in records if record.index == index),
                None,
            )
        if any(
            _NEGATED_CLAUSE.search(clause) and (_PREVIOUS_PHOTO.search(clause) or _GROUP_INDEX.search(clause))
            for clause in clauses
        ):
            return None
        return self.get(scope)

    def touch(self, record: PhotoRecord) -> None:
        """Select a retained photo without changing the order of its group."""
        current = self.get(record.scope, record.photo_id)
        if current is None or current.group_id != record.group_id:
            return
        self._selected[record.scope] = record.photo_id
        self._save()

    def describe(self, scope: PhotoScope) -> str:
        available = self._prune().get(scope)
        if available is None:
            return ""

        def visible_records(records):
            return [
                {
                    "photo_id": record.photo_id,
                    "group_id": record.group_id,
                    "index": record.index,
                    "photo_type": record.photo_type,
                    "mode": record.mode,
                    "description": record.description,
                    "shot": {key: record.shot[key] for key in SHOT_FIELDS},
                }
                for record in records
            ]

        latest, *older = available
        groups = self._groups[scope]
        data = {
            "selected_photo_id": self._selected[scope],
            "latest_group": {
                "label": "最新一批成片",
                "group_id": groups[0][0].group_id,
            },
            "photos": visible_records(latest),
        }
        if older:
            data["previous_group"] = {
                "label": "上一批成片",
                "group_id": groups[1][0].group_id,
                "photos": visible_records(older[0]),
            }
        return json.dumps(data, ensure_ascii=False)
