"""领域常量：角色、状态、监测通道与处置阶梯。

字段称谓与 fixtures/domain.json 保持一致；业务记录只能由正式引擎产生，
此处的常量仅用于统一语义。
"""

# 任务相关角色（权限视图按此区分）
ROLES = ("任务人员", "现场卫生员", "值班军医", "指挥人员")
ROLE_COMMANDER = "指挥人员"
ROLE_MEDIC = "值班军医"
ROLE_FIELD_MEDIC = "现场卫生员"
ROLE_WEARER = "任务人员"

# 参考状态（fixtures/domain.json 中的五项）
STATE_MONITORING = "监测中"
STATE_REVIEW = "需复核"
STATE_ALERTED = "已预警"
STATE_INTERVENING = "干预中"
STATE_CLEARED = "已解除"
STATES = (
    STATE_MONITORING,
    STATE_REVIEW,
    STATE_ALERTED,
    STATE_INTERVENING,
    STATE_CLEARED,
)

# 监测通道
CH_SPO2 = "spo2"
CH_TEMP = "core_temp"
CH_EEG = "eeg"
CHANNELS = (CH_SPO2, CH_TEMP, CH_EEG)
CHANNEL_LABELS = {CH_SPO2: "血氧", CH_TEMP: "核心体温", CH_EEG: "脑电"}

# 高危信号依次触发的处置阶梯（顺序即升级方向）
ACTION_REVIEW = "现场复核"
ACTION_COOL = "降温补水"
ACTION_EVACUATE = "转运"
ACTIONS = (ACTION_REVIEW, ACTION_COOL, ACTION_EVACUATE)
ACTION_LEVEL = {ACTION_REVIEW: 0, ACTION_COOL: 1, ACTION_EVACUATE: 2}

# 规则严重级别 -> 处置动作
SEVERITY_REVIEW = "review"
SEVERITY_COOL = "cool"
SEVERITY_EVACUATE = "evacuate"
SEVERITIES = (SEVERITY_REVIEW, SEVERITY_COOL, SEVERITY_EVACUATE)
SEVERITY_LEVEL = {
    SEVERITY_REVIEW: 0,
    SEVERITY_COOL: 1,
    SEVERITY_EVACUATE: 2,
}
SEVERITY_ACTION = {
    SEVERITY_REVIEW: ACTION_REVIEW,
    SEVERITY_COOL: ACTION_COOL,
    SEVERITY_EVACUATE: ACTION_EVACUATE,
}

# 数据质量标记：异常只允许降低置信度，不允许系统补造数据
Q_SENSOR_MISSING = "sensor_missing"        # 传感器失联
Q_READING_CONFLICT = "reading_conflict"    # 多传感器读数相互矛盾
Q_CLOCK_DRIFT = "clock_drift"              # 时间戳相对采集序列漂移
Q_OUT_OF_RANGE = "out_of_range"            # 超出物理合理域
Q_LATE = "late_beyond_cutoff"              # 超过截止延迟才到达
QUALITY_FLAGS = (
    Q_SENSOR_MISSING,
    Q_READING_CONFLICT,
    Q_CLOCK_DRIFT,
    Q_OUT_OF_RANGE,
    Q_LATE,
)
