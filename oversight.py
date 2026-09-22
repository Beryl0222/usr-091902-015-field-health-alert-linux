"""处置监督：信号升级、军医覆盖留痕、按角色裁剪视图。

* 高危信号按冻结规则的严重级别依次触发 现场复核 → 降温补水 → 转运，
  自动方向只升不降；每条处置都锚定产生它的窗口、模型版本、校准与阈值指纹。
* 值班军医可以覆盖建议（包括解除），但必须填写理由；覆盖本身不可变留痕。
* 任务结束后的阈值版本更新不会重写这里的任何旧记录——记录写死了当时指纹。
* 角色视图：指挥人员只见履职所需状态（谁、需要哪一级动作），看不到
  生命体征明细与完整健康档案；卫生员可见动作与复核要点；军医可见全部。
"""

from domain import (
    ACTION_LEVEL,
    ACTION_REVIEW,
    ACTION_COOL,
    ACTION_EVACUATE,
    ACTIONS,
    ROLE_COMMANDER,
    ROLE_FIELD_MEDIC,
    ROLE_MEDIC,
    ROLE_WEARER,
    STATE_ALERTED,
    STATE_CLEARED,
    STATE_INTERVENING,
    STATE_MONITORING,
    STATE_REVIEW,
)
from store import AppendOnlyStore

ACTION_CLEAR = "解除"

# 处置动作 -> 参考状态（与 fixtures/domain.json 对齐）
ACTION_STATE = {
    ACTION_REVIEW: STATE_REVIEW,
    ACTION_COOL: STATE_INTERVENING,
    ACTION_EVACUATE: STATE_ALERTED,
    ACTION_CLEAR: STATE_CLEARED,
}


class OversightError(ValueError):
    pass


class Oversight:
    def __init__(self, store=None):
        self.store = store or AppendOnlyStore(namespace="oversight")
        self._subjects = {}     # subject_id -> 当前处置状态
        self._windows = set()   # (subject_id, window_start) 已处理窗口

    def apply_assessment(self, assessment, ts=None):
        """消化一条已定稿评估；若构成升级则产生处置记录，否则返回 None。"""
        subject_id = assessment["subject_id"]
        window = assessment["window_start"]
        if (subject_id, window) in self._windows:
            return None
        self._windows.add((subject_id, window))

        subject = self._subjects.setdefault(
            subject_id, self._blank(subject_id)
        )
        action = assessment.get("recommended_action")
        if action is None:
            return None
        new_level = ACTION_LEVEL[action]
        # 自动方向只升不降；军医覆盖（含解除）后，新窗口出现更高风险可再次升级
        if new_level <= subject["level"]:
            return None

        trigger = self._trigger_basis(assessment)
        subject.update(level=new_level, action=action,
                       state=ACTION_STATE[action], source="auto",
                       window_start=window, actor=None, reason=None,
                       trigger=trigger)
        return self._log("auto_action", subject, ts,
                         note="按冻结规则逐级触发")

    def override(self, subject_id, medic_id, action, reason, ts=None):
        """值班军医覆盖建议。理由为强制项，且动作必须合法。"""
        if not medic_id:
            raise OversightError("覆盖必须记录值班军医身份")
        if not reason or not str(reason).strip():
            raise OversightError("军医覆盖必须留下理由")
        if action not in ACTIONS and action != ACTION_CLEAR:
            raise OversightError(f"不支持的处置动作：{action}")

        subject = self._subjects.setdefault(
            subject_id, self._blank(subject_id)
        )
        previous = {"action": subject["action"], "state": subject["state"],
                    "source": subject["source"], "reason": subject["reason"]}
        level = -1 if action == ACTION_CLEAR else ACTION_LEVEL[action]
        subject.update(level=level, action=(None if action == ACTION_CLEAR else action),
                       state=ACTION_STATE[action], source="medic_override",
                       actor=medic_id, reason=str(reason).strip(),
                       override_ts=ts)
        return self._log("medic_override", subject, ts,
                         previous=previous, note="军医覆盖，已记录理由")

    def _log(self, event_type, subject, ts, **extra):
        record = {
            "record_type": event_type,
            "subject_id": subject["subject_id"],
            "ts": ts,
            "action": subject["action"],
            "level": subject["level"],
            "state": subject["state"],
            "source": subject["source"],
            "actor": subject["actor"],
            "reason": subject["reason"],
            "window_start": subject.get("window_start"),
            "trigger": subject.get("trigger"),
        }
        record.update(extra)
        self.store.append("disposition", record)
        return record

    @staticmethod
    def _blank(subject_id):
        return {"subject_id": subject_id, "level": -1, "action": None,
                "state": STATE_MONITORING, "source": "initial",
                "actor": None, "reason": None, "window_start": None,
                "trigger": None}

    @staticmethod
    def _trigger_basis(a):
        return {
            "window_start": a["window_start"],
            "window_end": a["window_end"],
            "protocol_id": a["protocol_id"],
            "model_version": a["model_version"],
            "protocol_fingerprint": a["protocol_fingerprint"],
            "calibration_fingerprint": a["calibration_fingerprint"],
            "threshold_fingerprint": a["threshold_fingerprint"],
            "severity": a["severity"],
            "confidence": a["confidence"],
            "confidence_downgraded": a["confidence_downgraded"],
            "quality_flags": a["quality_flags"],
            "hits": a["hits"],
            "basis_refs": a["basis_refs"],
        }

    # ------------------------------------------------------------------
    # 审计与视图
    # ------------------------------------------------------------------
    def audit_log(self, subject_id=None):
        records = self.store.payloads("disposition")
        if subject_id is not None:
            records = [r for r in records if r["subject_id"] == subject_id]
        return records

    def subjects(self):
        return sorted(self._subjects)

    def status_of(self, subject_id):
        s = self._subjects.get(subject_id)
        if s is None:
            return None
        return {k: s[k] for k in
                ("subject_id", "level", "action", "state", "source",
                 "actor", "reason", "window_start")}

    def view_for(self, role, subject_id=None):
        """按履职需要裁剪：角色越低，看到的健康信息越少。"""
        ids = [subject_id] if subject_id else self.subjects()
        if role == ROLE_COMMANDER:
            return [self._commander_view(sid) for sid in ids]
        if role == ROLE_FIELD_MEDIC:
            return [self._field_view(sid) for sid in ids]
        if role == ROLE_MEDIC:
            return [self._medic_view(sid) for sid in ids]
        if role == ROLE_WEARER:
            return [self._wearer_view(sid) for sid in ids]
        raise OversightError(f"未知角色：{role}")

    def _commander_view(self, sid):
        # 只有履职状态：谁、处于什么状态、需要哪一级动作；无任何体征明细
        s = self._subjects[sid]
        return {
            "subject_id": sid,
            "state": s["state"],
            "required_action": s["action"],
            "since_window": s["window_start"],
            "overridden": s["source"] == "medic_override",
        }

    def _field_view(self, sid):
        # 卫生员：动作 + 现场复核要点（通道、质量标记、依据），无完整档案
        s = self._subjects[sid]
        view = self._commander_view(sid)
        trigger = s.get("trigger") or {}
        view["review_points"] = [
            {"channel": h["channel"], "metric": h["metric"],
             "observed": h["observed"], "threshold": h["threshold"]}
            for h in trigger.get("hits", [])
        ]
        view["quality_flags"] = trigger.get("quality_flags", [])
        view["confidence_downgraded"] = trigger.get("confidence_downgraded", False)
        view["basis_refs"] = trigger.get("basis_refs", [])
        return view

    def _medic_view(self, sid):
        # 军医：当前状态 + 触发依据 + 完整处置/覆盖审计
        s = self._subjects[sid]
        return {
            "subject_id": sid,
            "current": self.status_of(sid),
            "trigger": s.get("trigger"),
            "audit": self.audit_log(sid),
        }

    def _wearer_view(self, sid):
        # 本人：只收到面向自己的动作提示，不展示风险档案
        s = self._subjects[sid]
        return {"subject_id": sid, "state": s["state"],
                "instruction": s["action"]}
