-- Register EST C00/V00 as standard single-node charts for the current engine.
-- The migration is idempotent and intentionally writes to formula, because
-- charts_loader.py does not use formula4 as its runtime chart source.

BEGIN IMMEDIATE;

INSERT INTO formula (
    序号,
    CategoryID,
    SectionNumber,
    ChartNumber,
    SectionNumber_R,
    动作代码,
    Chartcode,
    ChartTitle,
    ChartFormula,
    ChartValueAdded,
    ChartToThreeDecimals,
    ChartIsVehicleAssembly,
    ChartDevelopedInSeconds,
    ChartStatus,
    VariableID,
    VariableNumber,
    VariableTitle,
    RangeID,
    RangeNumber,
    ValueID,
    ValueNumber,
    ValueDescription,
    ValueEnglishAbbrev,
    ValueMetricAbbrev,
    ValueNextVariable,
    ValueNextRange,
    ValueFormulaValue
)
SELECT
    COALESCE((SELECT MAX(序号) FROM formula), 0) + 1,
    0, 0, 'C00', 'EST', '000',
    'EST C00', '固定估算工时（C）', 'V1',
    1, 0, 0, 1, 1,
    0, 1, '固定估算秒数', 0, 1, 0, 1,
    '固定5秒', '5S', '5S', 0, 0, 5.0
WHERE NOT EXISTS (
    SELECT 1 FROM formula WHERE Chartcode = 'EST C00'
);

INSERT INTO formula (
    序号,
    CategoryID,
    SectionNumber,
    ChartNumber,
    SectionNumber_R,
    动作代码,
    Chartcode,
    ChartTitle,
    ChartFormula,
    ChartValueAdded,
    ChartToThreeDecimals,
    ChartIsVehicleAssembly,
    ChartDevelopedInSeconds,
    ChartStatus,
    VariableID,
    VariableNumber,
    VariableTitle,
    RangeID,
    RangeNumber,
    ValueID,
    ValueNumber,
    ValueDescription,
    ValueEnglishAbbrev,
    ValueMetricAbbrev,
    ValueNextVariable,
    ValueNextRange,
    ValueFormulaValue
)
SELECT
    COALESCE((SELECT MAX(序号) FROM formula), 0) + 1,
    0, 0, 'V00', 'EST', '000',
    'EST V00', '固定估算工时（V）', 'V1',
    0, 0, 0, 1, 1,
    0, 1, '固定估算秒数', 0, 1, 0, 1,
    '固定5秒', '5S', '5S', 0, 0, 5.0
WHERE NOT EXISTS (
    SELECT 1 FROM formula WHERE Chartcode = 'EST V00'
);

INSERT INTO formula_chart (序号, 动作代码, 标题, 公式)
SELECT
    COALESCE((SELECT MAX(序号) FROM formula_chart), 0) + 1,
    'EST C00',
    '固定估算工时（C）',
    'V1'
WHERE NOT EXISTS (
    SELECT 1 FROM formula_chart WHERE 动作代码 = 'EST C00'
);

INSERT INTO formula_chart (序号, 动作代码, 标题, 公式)
SELECT
    COALESCE((SELECT MAX(序号) FROM formula_chart), 0) + 1,
    'EST V00',
    '固定估算工时（V）',
    'V1'
WHERE NOT EXISTS (
    SELECT 1 FROM formula_chart WHERE 动作代码 = 'EST V00'
);

COMMIT;
