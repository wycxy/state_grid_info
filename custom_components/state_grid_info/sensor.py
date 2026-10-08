"""Sensor platform for State Grid Info integration."""

import logging
import math
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CONSUMER_NAME,
    CONF_CONSUMER_NUMBER,
    CONF_DATA_SOURCE,
    CONF_IS_PREPAID,
    DATA_SOURCE_GAS,
    DOMAIN,
    GAS_BILLING_NAMES,
    GAS_BILLING_YEAR_阶梯,
    GAS_BILLING_MONTH_阶梯,
    GAS_BILLING_平均单价,
    CONF_GAS_BILLING_STANDARD,
    CONF_GAS_LADDER_LEVEL_1,
    CONF_GAS_LADDER_LEVEL_2,
    CONF_GAS_LADDER_PRICE_1,
    CONF_GAS_LADDER_PRICE_2,
    CONF_GAS_LADDER_PRICE_3,
    CONF_GAS_YEAR_LADDER_START,
    CONF_GAS_AVERAGE_PRICE,
)
from .coordinator import StateGridInfoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up State Grid Info sensors from a config entry."""
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(
        entry.entry_id,
        {"config": entry.data, "entities": []},
    )

    # 燃气数据源：仅创建燃气实体，数据来自 xinaogas 集成
    if entry.data.get(CONF_DATA_SOURCE) == DATA_SOURCE_GAS:
        gas_sensor = StateGridGasSensor(hass, entry.data)
        entry_data["entities"] = [gas_sensor]
        async_add_entities([gas_sensor], True)
        return

    coordinator: StateGridInfoCoordinator | None = entry_data.get("coordinator")
    if coordinator is None:
        coordinator = StateGridInfoCoordinator(hass, entry.data)
        entry_data["coordinator"] = coordinator

    await coordinator.async_prepare()
    await coordinator.async_config_entry_first_refresh()

    entities: list[SensorEntity] = [
        StateGridInfoOverviewSensor(coordinator, entry.data),
        StateGridInfoTotalEnergySensor(coordinator, entry.data),
        StateGridInfoCurrentMonthEnergySensor(coordinator, entry.data),
        StateGridInfoCurrentMonthCostSensor(coordinator, entry.data),
        StateGridInfoTotalCostSensor(coordinator, entry.data),
    ]
    entry_data["entities"] = entities
    async_add_entities(entities)


class StateGridInfoBaseSensor(CoordinatorEntity, SensorEntity):
    """Common base entity backed by the shared coordinator."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: StateGridInfoCoordinator,
        config: dict[str, Any],
        *,
        unique_suffix: str,
        entity_suffix: str,
        name_suffix: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self.config = config
        self._consumer_number = str(config.get(CONF_CONSUMER_NUMBER, ""))
        self._attr_unique_id = f"state_grid_{self._consumer_number}{unique_suffix}"
        self.entity_id = f"sensor.state_grid_{self._consumer_number}{entity_suffix}"
        base_name = f"国家电网 {self._consumer_number}"
        self._attr_name = base_name if not name_suffix else f"{base_name} {name_suffix}"
        self._attr_icon = icon

    @property
    def available(self) -> bool:
        """Return whether the entity is available.

        State Grid data is delayed by design, so previously cached data should
        remain available even when no fresh payload arrives for a long period.
        """
        return bool(self.coordinator.runtime_snapshot or self.coordinator.data)

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the shared device info."""
        snapshot = self.coordinator.runtime_snapshot or {}
        consumer_name = snapshot.get("consumer_name") or self.config.get(CONF_CONSUMER_NAME, "")
        return {
            "identifiers": {(DOMAIN, f"state_grid_{self._consumer_number}")},
            "name": f"国家电网 {self._consumer_number}",
            "manufacturer": "国家电网",
            "model": f"户名:{consumer_name}",
        }

    def _snapshot(self) -> dict[str, Any]:
        return self.coordinator.runtime_snapshot or {}


class StateGridInfoOverviewSensor(StateGridInfoBaseSensor):
    """Primary overview sensor kept for UI card compatibility."""

    _attr_native_unit_of_measurement = "元"

    def __init__(self, coordinator: StateGridInfoCoordinator, config: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            config,
            unique_suffix="",
            entity_suffix="",
            name_suffix="",
            icon="mdi:flash",
        )

    @property
    def native_value(self) -> float:
        """Return the account balance.

        预付费户号：当余额为 0（数据源未给出有效余额）时，取上月电费作为实体值。
        """
        snapshot = self._snapshot()
        balance = self._raw_balance(snapshot)
        if balance == 0.0 and self.config.get(CONF_IS_PREPAID, False):
            return float(snapshot.get("last_month_ele_cost", 0.0))
        return balance

    @staticmethod
    def _raw_balance(snapshot: dict[str, Any]) -> float:
        """Return the untouched account balance from the snapshot."""
        return float(snapshot.get("balance", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return card-compatible overview attributes."""
        snapshot = self._snapshot()
        overview = snapshot.get("overview", {})
        billing = snapshot.get("billing", {})
        daylist = overview.get("daylist", [])

        attrs: dict[str, Any] = {
            "date": snapshot.get("date", ""),
            "daylist": daylist,
            "monthlist": overview.get("monthlist", []),
            "yearlist": overview.get("yearlist", []),
            "rechargelist": snapshot.get("rechargelist", []),
            "数据源": self.config.get(CONF_DATA_SOURCE, "unknown"),
        }

        if daylist:
            recent_days = daylist[:7]
            daily_costs = [float(day.get("dayEleCost", 0)) for day in recent_days]
            if daily_costs:
                avg_daily_cost = sum(daily_costs) / len(daily_costs)
                attrs["日均消费"] = round(avg_daily_cost, 2)
                if avg_daily_cost > 0:
                    try:
                        latest_date = datetime.strptime(daylist[0]["day"], "%Y-%m-%d")
                        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                        days_since_latest = (today - latest_date).days
                        # 剩余天数始终基于真实余额计算，不受预付费「取上月电费」的
                        # 实体值替换影响。
                        remaining_days = max(
                            0.0,
                            self._raw_balance(snapshot) / avg_daily_cost - days_since_latest,
                        )
                        attrs["剩余天数"] = math.ceil(remaining_days)
                    except (KeyError, ValueError) as exc:
                        _LOGGER.debug("Failed to calculate remaining prepaid days: %s", exc)

        attrs["预付费"] = billing.get("is_prepaid", "否")

        billing_attrs = dict(billing.get("config", {}))
        billing_attrs.update(billing.get("ladder_info", {}))
        attrs["计费标准"] = billing_attrs

        last_sync_at = snapshot.get("last_sync_at", "")
        if last_sync_at:
            try:
                attrs["最后同步日期"] = datetime.fromisoformat(last_sync_at).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                attrs["最后同步日期"] = last_sync_at
        else:
            attrs["最后同步日期"] = self.coordinator.last_update_time.strftime("%Y-%m-%d %H:%M:%S")

        return attrs


class StateGridInfoTotalEnergySensor(StateGridInfoBaseSensor):
    """Lifetime cumulative energy for Home Assistant Energy dashboard."""

    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: StateGridInfoCoordinator, config: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            config,
            unique_suffix="_total_energy",
            entity_suffix="_total_energy",
            name_suffix="累计用电",
            icon="mdi:lightning-bolt",
        )

    @property
    def native_value(self) -> float:
        snapshot = self._snapshot()
        return float(snapshot.get("energy", {}).get("total_energy_kwh", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        energy = snapshot.get("energy", {})
        cost = snapshot.get("cost", {})
        return {
            "最后结算日期": energy.get("last_official_day", ""),
            "当月用电": energy.get("current_month_kwh", 0.0),
            "当年用电": energy.get("current_year_kwh", 0.0),
            "累计电费": cost.get("total_cost", 0.0),
        }


class StateGridInfoCurrentMonthEnergySensor(StateGridInfoBaseSensor):
    """Current month electricity usage."""

    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(self, coordinator: StateGridInfoCoordinator, config: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            config,
            unique_suffix="_current_month_energy",
            entity_suffix="_current_month_energy",
            name_suffix="当月用电",
            icon="mdi:calendar-month",
        )

    @property
    def native_value(self) -> float:
        snapshot = self._snapshot()
        return float(snapshot.get("energy", {}).get("current_month_kwh", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        energy = snapshot.get("energy", {})
        return {
            "最后结算日期": energy.get("last_official_day", ""),
        }


class StateGridInfoCurrentMonthCostSensor(StateGridInfoBaseSensor):
    """Current month electricity cost."""

    _attr_native_unit_of_measurement = "元"

    def __init__(self, coordinator: StateGridInfoCoordinator, config: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            config,
            unique_suffix="_current_month_cost",
            entity_suffix="_current_month_cost",
            name_suffix="当月电费",
            icon="mdi:cash",
        )

    @property
    def native_value(self) -> float:
        snapshot = self._snapshot()
        return float(snapshot.get("cost", {}).get("current_month_cost", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        cost = snapshot.get("cost", {})
        return {
            "当年电费": cost.get("current_year_cost", 0.0),
            "累计电费": cost.get("total_cost", 0.0),
            "电费是否预估": cost.get("current_month_cost_is_estimated", False),
        }


class StateGridInfoTotalCostSensor(StateGridInfoBaseSensor):
    """Lifetime cumulative electricity cost.

    同时作为长期统计数据源：``device_class = MONETARY`` 搭配
    ``state_class = TOTAL``（HA 中 monetary 只允许 total），Recorder 会为它
    生成 statistics，可直接在「能源」面板 → 电网 → 成本跟踪里选择
    「使用跟踪总成本的实体」（stat_cost）。HA 自带的 EnergyCostSensor 用的
    也是这组属性，只是单位换成 HA 配置的货币代码。
    """

    _attr_native_unit_of_measurement = "元"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: StateGridInfoCoordinator, config: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            config,
            unique_suffix="_total_cost",
            entity_suffix="_total_cost",
            name_suffix="累计电费",
            icon="mdi:cash-multiple",
        )

    @property
    def native_value(self) -> float:
        snapshot = self._snapshot()
        return float(snapshot.get("cost", {}).get("total_cost", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        cost = snapshot.get("cost", {})
        energy = snapshot.get("energy", {})
        return {
            "当月电费": cost.get("current_month_cost", 0.0),
            "当年电费": cost.get("current_year_cost", 0.0),
            "累计用电": energy.get("total_energy_kwh", 0.0),
        }


class StateGridGasSensor(SensorEntity):
    """基于 xinaogas 集成数据的燃气传感器，实体格式与电费实体保持一致。"""

    def __init__(self, hass, config):
        """Initialize the gas sensor."""
        self._hass = hass
        self.config = config
        payment_no = config.get(CONF_CONSUMER_NUMBER, "")
        entity_slug = payment_no.lower() if payment_no else "unknown"
        self.entity_id = f"sensor.xinao_gas_{entity_slug}"
        self._attr_unique_id = f"xinao_gas_{payment_no}"
        self._attr_icon = "mdi:fire"
        self._attr_name = f"新奥燃气 {payment_no}"
        self._attr_native_unit_of_measurement = "元"
        self._payment_no = payment_no

    def _get_gas_data(self):
        """从 xinaogas 集成获取匹配当前缴费号的燃气数据。"""
        xinaogas_domain = self._hass.data.get("xinaogas", {})
        coordinators = xinaogas_domain.get("coordinators", {})
        for coordinator in coordinators.values():
            data = coordinator.data or {}
            if str(data.get("payment_no") or "").strip() == self._payment_no:
                return data
        # 兼容旧式存储：取第一个可用数据
        if coordinators:
            coordinator = list(coordinators.values())[0]
            return coordinator.data or {}
        for key, value in xinaogas_domain.items():
            if key in ("token_manager", "coordinators"):
                continue
            if hasattr(value, "data") and isinstance(value.data, dict):
                return value.data
        return {}

    @property
    def device_info(self):
        """Return device info."""
        data = self._get_gas_data()
        consumer_name = data.get("user_name", "") or data.get("family_account_name", "")
        payment_no_val = data.get("payment_no", "") or self._payment_no
        name = f"新奥燃气 {payment_no_val}"
        return {
            "identifiers": {(DOMAIN, f"xinao_gas_{self._payment_no}")},
            "name": name,
            "manufacturer": "新奥燃气",
            "model": f"户名:{consumer_name}" if consumer_name else "新奥燃气",
        }

    @property
    def available(self):
        """Return if entity is available."""
        return bool(self._get_gas_data())

    @property
    def native_value(self):
        """Return the state of the sensor."""
        data = self._get_gas_data()
        if not data:
            return 0
        return data.get("balance", 0)

    def _get_ladder_period_usage(self, billing, dayList, monthList, yearList):
        """根据计费标准计算当前阶梯周期内的用气量。

        年阶梯：取当年 yearList 中的用量
        月阶梯：取当月 monthList 中的用量
        """
        import datetime as _dt
        today = _dt.date.today()
        current_year = str(today.year)
        current_month = today.strftime("%Y-%m")

        if billing == GAS_BILLING_YEAR_阶梯:
            for item in yearList:
                if str(item.get("year")) == current_year:
                    return float(item.get("yearEleNum", 0) or 0)
        elif billing == GAS_BILLING_MONTH_阶梯:
            for item in monthList:
                if str(item.get("month")) == current_month:
                    return float(item.get("monthEleNum", 0) or 0)
        return 0.0

    def _get_gas_price(self, ladder_period_usage):
        """根据配置的阶梯计费标准和当前阶梯周期用量计算当前气价。"""
        billing = self.config.get(CONF_GAS_BILLING_STANDARD, "")
        if billing == GAS_BILLING_平均单价:
            return self.config.get(CONF_GAS_AVERAGE_PRICE, 2.65)
        elif billing in (GAS_BILLING_YEAR_阶梯, GAS_BILLING_MONTH_阶梯):
            level1 = self.config.get(CONF_GAS_LADDER_LEVEL_1, 360)
            level2 = self.config.get(CONF_GAS_LADDER_LEVEL_2, 600)
            price1 = self.config.get(CONF_GAS_LADDER_PRICE_1, 2.65)
            price2 = self.config.get(CONF_GAS_LADDER_PRICE_2, 2.85)
            price3 = self.config.get(CONF_GAS_LADDER_PRICE_3, 3.50)
            if ladder_period_usage < level1:
                return price1
            elif ladder_period_usage < level2:
                return price2
            else:
                return price3
        return self.config.get(CONF_GAS_AVERAGE_PRICE, 2.65)

    def _get_billing_standard_attrs(self, ladder_period_usage):
        """获取计费标准属性，格式与电费实体一致，嵌套在'计费标准'下。"""
        billing = self.config.get(CONF_GAS_BILLING_STANDARD, "")
        attrs = {}
        attrs["计费标准"] = GAS_BILLING_NAMES.get(billing, "")

        if billing == GAS_BILLING_平均单价:
            attrs["平均气价"] = self.config.get(CONF_GAS_AVERAGE_PRICE, 2.65)
        elif billing == GAS_BILLING_YEAR_阶梯:
            level1 = self.config.get(CONF_GAS_LADDER_LEVEL_1, 360)
            level2 = self.config.get(CONF_GAS_LADDER_LEVEL_2, 600)
            price1 = self.config.get(CONF_GAS_LADDER_PRICE_1, 2.65)
            price2 = self.config.get(CONF_GAS_LADDER_PRICE_2, 2.85)
            price3 = self.config.get(CONF_GAS_LADDER_PRICE_3, 3.50)
            attrs["年阶梯第2档起始气量"] = level1
            attrs["年阶梯第3档起始气量"] = level2
            attrs["年阶梯第1档气价"] = price1
            attrs["年阶梯第2档气价"] = price2
            attrs["年阶梯第3档气价"] = price3
            # 年阶梯周期
            start_mmdd = self.config.get(CONF_GAS_YEAR_LADDER_START, "0101")
            import datetime as _dt
            today = _dt.date.today()
            year = today.year
            try:
                month = int(start_mmdd[:2])
                day = int(start_mmdd[2:4])
                start_date = _dt.date(year, month, day)
                end_date = _dt.date(year, 12, 31)
                attrs["当前年阶梯起始日期"] = start_date.strftime("%Y.%m.%d")
                attrs["当前年阶梯结束日期"] = end_date.strftime("%Y.%m.%d")
            except (ValueError, IndexError):
                pass
            # 当前阶梯档（基于年内累计用气量）
            if ladder_period_usage <= level1:
                attrs["当前年阶梯档"] = "第1档"
            elif ladder_period_usage <= level2:
                attrs["当前年阶梯档"] = "第2档"
            else:
                attrs["当前年阶梯档"] = "第3档"
            attrs["年阶梯累计用气量"] = round(ladder_period_usage, 2)
        elif billing == GAS_BILLING_MONTH_阶梯:
            level1 = self.config.get(CONF_GAS_LADDER_LEVEL_1, 30)
            level2 = self.config.get(CONF_GAS_LADDER_LEVEL_2, 50)
            price1 = self.config.get(CONF_GAS_LADDER_PRICE_1, 2.65)
            price2 = self.config.get(CONF_GAS_LADDER_PRICE_2, 2.85)
            price3 = self.config.get(CONF_GAS_LADDER_PRICE_3, 3.50)
            attrs["月阶梯第2档起始气量"] = level1
            attrs["月阶梯第3档起始气量"] = level2
            attrs["月阶梯第1档气价"] = price1
            attrs["月阶梯第2档气价"] = price2
            attrs["月阶梯第3档气价"] = price3
            # 当前阶梯档（基于月内累计用气量）
            if ladder_period_usage <= level1:
                attrs["当前月阶梯档"] = "第1档"
            elif ladder_period_usage <= level2:
                attrs["当前月阶梯档"] = "第2档"
            else:
                attrs["当前月阶梯档"] = "第3档"
            attrs["月阶梯累计用气量"] = round(ladder_period_usage, 2)

        return attrs

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        data = self._get_gas_data()
        if not data:
            return {}

        attrs = {}

        # 使用 daily_usage_all（180天）而非 daily_usage_map（30天）
        daily_map = data.get("daily_usage_all", {}) or data.get("daily_usage_map", {}) or {}
        meter_reading = float(data.get("meter_reading", 0) or 0)

        # 先用 xinaogas 返回的 gas_price 做初步计算，后面再根据阶梯调整
        raw_gas_price = float(data.get("gas_price", 0) or 0)

        # 将 xinaogas 的每日用气数据转换为与电费实体一致的 dayList 格式
        dayList = []
        for day, usage in sorted(daily_map.items()):
            usage_val = float(usage) if usage is not None else 0.0
            dayList.append({
                "day": day,
                "dayEleNum": round(usage_val, 3),
                "dayEleCost": round(usage_val * raw_gas_price, 2) if raw_gas_price else 0.0,
                "dayTPq": 0,
                "dayPPq": 0,
                "dayNPq": 0,
                "dayVPq": 0,
            })
        dayList.reverse()  # 最新日期在前

        # 由 dayList 汇总生成 monthList（使用 round 避免浮点精度问题）
        month_map = {}
        for item in dayList:
            month = item["day"][:7]
            if month not in month_map:
                month_map[month] = {
                    "month": month, "monthEleNum": 0.0, "monthEleCost": 0.0,
                    "monthTPq": 0, "monthPPq": 0, "monthNPq": 0, "monthVPq": 0,
                }
            month_map[month]["monthEleNum"] = round(month_map[month]["monthEleNum"] + item["dayEleNum"], 3)
            month_map[month]["monthEleCost"] = round(month_map[month]["monthEleCost"] + item["dayEleCost"], 2)
        monthList = sorted(month_map.values(), key=lambda x: x["month"], reverse=True)

        # 由 monthList 汇总生成 yearList（使用 round 避免浮点精度问题）
        year_map = {}
        for item in monthList:
            year = item["month"][:4]
            if year not in year_map:
                year_map[year] = {
                    "year": year, "yearEleNum": 0.0, "yearEleCost": 0.0,
                    "yearTPq": 0, "yearPPq": 0, "yearNPq": 0, "yearVPq": 0,
                }
            year_map[year]["yearEleNum"] = round(year_map[year]["yearEleNum"] + item["monthEleNum"], 3)
            year_map[year]["yearEleCost"] = round(year_map[year]["yearEleCost"] + item["monthEleCost"], 2)
        yearList = sorted(year_map.values(), key=lambda x: x["year"], reverse=True)

        # 根据计费标准计算当前阶梯周期内的用气量（非表读数）
        billing = self.config.get(CONF_GAS_BILLING_STANDARD, "")
        ladder_period_usage = self._get_ladder_period_usage(billing, dayList, monthList, yearList)
        gas_price = self._get_gas_price(ladder_period_usage)

        # 计算日均消费（最近7天）
        if dayList:
            recent_days = dayList[:7]
            daily_costs = [day.get("dayEleCost", 0) for day in recent_days]
            avg_daily_cost = sum(daily_costs) / len(daily_costs) if daily_costs else 0
            attrs["日均消费"] = round(avg_daily_cost, 2)

        attrs.update({
            "date": data.get("last_update_time", ""),
            "daylist": dayList,
            "monthlist": monthList,
            "yearlist": yearList,
        })

        # 计费标准属性（与电费实体格式一致，嵌套在"计费标准"下，供卡片显示阶梯信息）
        attrs["计费标准"] = self._get_billing_standard_attrs(ladder_period_usage)

        # 燃气专属属性
        attrs["燃气单价"] = gas_price
        attrs["当月用气量"] = data.get("current_month_usage", 0)
        attrs["当月气费"] = round(float(data.get("current_month_amount", 0) or 0), 2)
        attrs["当月累计气量"] = data.get("month_total_gas", 0)
        attrs["累计用气"] = meter_reading
        attrs["抄表数"] = meter_reading
        attrs["上次抄表数"] = data.get("last_meter_reading", 0)
        attrs["抄表日期"] = data.get("meter_reading_date", "未知")
        attrs["最近账单日期"] = data.get("latest_bill_date", "未知")
        attrs["账单状态"] = data.get("bill_status", "未知")
        attrs["燃气公司"] = data.get("company_name", "未知")
        attrs["阀门状态"] = data.get("valve_status", "未知")
        attrs["电池状态"] = data.get("battery_status", "未知")
        attrs["物联表更新时间"] = data.get("iot_update_time", "未知")
        attrs["燃气表类型"] = data.get("meter_type", "未知")
        attrs["数据源"] = "xinaogas"
        attrs["最后同步日期"] = data.get("last_update_time", "")

        return attrs
