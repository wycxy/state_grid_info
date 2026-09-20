"""Persistent storage for State Grid Info integration."""
import json
import logging
import os
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _is_day_all_zero(item):
    """判断单条日用电数据是否全为 0（日用电量、电费、尖峰平谷各段均为 0）。"""
    if not isinstance(item, dict):
        return False
    return (
        item.get("dayEleNum", 0) == 0
        and item.get("dayEleCost", 0) == 0
        and item.get("dayTPq", 0) == 0
        and item.get("dayPPq", 0) == 0
        and item.get("dayNPq", 0) == 0
        and item.get("dayVPq", 0) == 0
    )


def _trim_consecutive_zero_days(day_list):
    """删除 dayList 首尾连续的全 0 日数据，保留中间有效区段。

    dayList 约定为「最新日期在前」。国网日用电列表在月初/月末常出现连续全 0
    （未来占位日、未抄表日），这些条目无统计与展示意义。参照 state_grid_app
    的 recent_30_daily_ele_list 处理，剔除首尾连续全 0 的天数：

    - 首部连续全 0：最新端（如当月尚未到的未来日 ``2026-09-30``）连续为 0 → 删除；
    - 尾部连续全 0：最旧端（如开户前）连续为 0 → 删除。
    """
    if not day_list:
        return day_list
    n = len(day_list)
    lead = 0
    while lead < n and _is_day_all_zero(day_list[lead]):
        lead += 1
    if lead == n:
        # 全部为 0，返回空列表
        return []
    tail = n - 1
    while tail > lead and _is_day_all_zero(day_list[tail]):
        tail -= 1
    if lead == 0 and tail == n - 1:
        return day_list
    return day_list[lead : tail + 1]


class StateGridStorage:
    """Manage persistent JSON storage for state grid data.

    Rules:
    - Data can only be added, never deleted.
    - Existing entries can be updated with new values.
    - dayList is merged by "day" key.
    - monthList is merged by "month" key.
    - yearList is merged by "year" key.
    """

    def __init__(self, hass, consumer_number: str):
        """Initialize storage."""
        self._hass = hass
        self._consumer_number = consumer_number
        self._file_path = hass.config.path(f"state_grid_info_{consumer_number}.json")
        self._data: dict[str, Any] = {}

    @property
    def file_path(self) -> str:
        """Return the JSON file path."""
        return self._file_path

    @property
    def data(self) -> dict[str, Any]:
        """Return current stored data."""
        return self._data

    def _load_sync(self) -> None:
        """Load data from JSON file (sync, must run in executor)."""
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                _LOGGER.info(
                    "已加载持久化数据: %s (dayList=%d条, monthList=%d条, yearList=%d条)",
                    self._file_path,
                    len(self._data.get("dayList", [])),
                    len(self._data.get("monthList", [])),
                    len(self._data.get("yearList", [])),
                )
            else:
                self._data = {
                    "date": "",
                    "balance": 0,
                    "dayList": [],
                    "monthList": [],
                    "yearList": [],
                    "consumer_name": "",
                }
                _LOGGER.info("持久化文件不存在，初始化空数据: %s", self._file_path)
        except (json.JSONDecodeError, IOError) as ex:
            _LOGGER.error("加载持久化数据失败: %s", ex)
            self._data = {
                "date": "",
                "balance": 0,
                "dayList": [],
                "monthList": [],
                "yearList": [],
                "consumer_name": "",
            }

    async def async_load(self) -> None:
        """Load data from JSON file asynchronously."""
        await self._hass.async_add_executor_job(self._load_sync)

    def _save_sync(self) -> None:
        """Save data to JSON file (sync, must run in executor)."""
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            _LOGGER.debug("已保存持久化数据: %s", self._file_path)
        except IOError as ex:
            _LOGGER.error("保存持久化数据失败: %s", ex)

    def _merge_list_by_key(self, existing: list, new_items: list, key: str) -> list:
        """Merge two lists by a key field.

        - New items are added.
        - Existing items (matched by key) are updated with new values.
        - Items only in existing are kept (never deleted).
        """
        existing_map = {item[key]: item for item in existing}
        for item in new_items:
            k = item[key]
            if k in existing_map:
                existing_map[k].update(item)
            else:
                existing_map[k] = item
        return sorted(existing_map.values(), key=lambda x: x[key], reverse=True)

    def update(self, new_data: dict[str, Any]) -> dict[str, Any]:
        """Update storage with new data, then return merged result.

        - dayList: merge by "day"
        - monthList: merge by "month"
        - yearList: merge by "year"
        - Scalar fields (date, balance, consumer_name): always update

        Note: This method does synchronous file I/O via _save_sync.
        It must be called via hass.async_add_executor_job from async code.
        """
        if not new_data:
            return self._data

        # Merge scalar fields - always update
        self._data["date"] = new_data.get("date", self._data.get("date", ""))
        self._data["balance"] = new_data.get("balance", self._data.get("balance", 0))
        self._data["consumer_name"] = new_data.get(
            "consumer_name", self._data.get("consumer_name", "")
        )

        # Merge dayList by "day"
        if "dayList" in new_data:
            self._data["dayList"] = self._merge_list_by_key(
                self._data.get("dayList", []), new_data["dayList"], "day"
            )
            # 合并后再次剔除首尾连续全0日：防止历史持久化数据中残留的占位全0天
            # 在「按 day 合并、旧条目不删」的规则下被长期保留。确保落盘与返回的
            # dayList 始终不含连续为空数据（与 _process_*_data 口径一致）。
            self._data["dayList"] = _trim_consecutive_zero_days(self._data["dayList"])

        # Merge monthList by "month"
        if "monthList" in new_data:
            self._data["monthList"] = self._merge_list_by_key(
                self._data.get("monthList", []), new_data["monthList"], "month"
            )

        # Merge yearList by "year"
        if "yearList" in new_data:
            self._data["yearList"] = self._merge_list_by_key(
                self._data.get("yearList", []), new_data["yearList"], "year"
            )

        # Merge rechargeList（充值记录）：按 (pay_date, amount, remark) 去重累积，
        # 始终按时间倒序。App 源每次刷新会重新抓取近 1 年并已在源头去重排序，
        # 这里以新数据为主、旧数据兜底——只有当 key 真正出现在新数据中才更新，
        # 刷新失败（new_data 不含该 key）或新数据为空时保留既有记录，避免清空历史。
        if "rechargeList" in new_data:
            new_records = new_data.get("rechargeList") or []
            if isinstance(new_records, list):
                old_records = self._data.get("rechargeList", []) or []
                if not isinstance(old_records, list):
                    old_records = []
                seen = set()
                merged: list[dict[str, Any]] = []
                for item in new_records + old_records:
                    if not isinstance(item, dict):
                        continue
                    rkey = (item.get("pay_date"), item.get("amount"), item.get("remark"))
                    if rkey in seen:
                        continue
                    seen.add(rkey)
                    merged.append(item)
                merged.sort(key=lambda x: str(x.get("pay_date") or ""), reverse=True)
                self._data["rechargeList"] = merged

        # Save to file
        self._save_sync()

        _LOGGER.info(
            "数据已合并并持久化: dayList=%d条, monthList=%d条, yearList=%d条",
            len(self._data.get("dayList", [])),
            len(self._data.get("monthList", [])),
            len(self._data.get("yearList", [])),
        )

        return dict(self._data)
