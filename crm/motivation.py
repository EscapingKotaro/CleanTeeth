from decimal import Decimal, ROUND_HALF_UP

# ============================================================
# КОНФИГ МОТИВАЦИИ (ТЗ п.14: формулы вынесены в конфигурацию).
# Меняется мотивационная модель — правим только этот файл.
# ============================================================
BONUS_1_RATE = Decimal("0.007")                    # 0,7% от «Итого КПЛ для расчёта»
BONUS_2_RATE = Decimal("0.002")                    # 0,2% от «Итого заказ-нарядов»
BONUS_3_PENALTY_RATE = Decimal("0.2") * BONUS_1_RATE   # −0,2 × 0,7% от (план − КПЛ) при <100%
BONUS_3_OVER_RATE = Decimal("0.2")                 # +20% от Бонуса 1 при ≥110%
BONUS_3_THRESHOLD_LOW = Decimal("100")             # граница штрафа, %
BONUS_3_THRESHOLD_HIGH = Decimal("110")            # граница премиальной ветки, %
DAILY_RATE = Decimal("1000")                       # ₽ за рабочий день (компонент выплаты)


def rub(value: Decimal) -> Decimal:
    """Округление до рубля, как в Google-файле."""
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def calculate_bonuses(monthly_plan):
    """
    Бонус 1 / 2 / 3 по формулам Google-файла мотивации.
    Возвращает НЕокруглённые Decimal (округляем на отображении и в итогах).
    """
    plan = monthly_plan.plan_amount or Decimal(0)
    k1 = monthly_plan.fact_amount or Decimal(0)
    kpl = monthly_plan.kpl_for_calc or Decimal(0)
    workorders = monthly_plan.workorders_amount or Decimal(0)

    # Бонус 1 = 0,7% × Итого КПЛ для расчёта
    bonus_1 = BONUS_1_RATE * kpl

    # Бонус 2 = 0,2% × Итого заказ-нарядов
    bonus_2 = BONUS_2_RATE * workorders

    # Выполнение = К1 / план × 100
    if plan > 0:
        completion = k1 / plan * 100
    else:
        # план не задан: считаем 0% (штрафная ветка), если К1 тоже 0
        completion = Decimal(0) if k1 <= 0 else Decimal("Infinity")

    # Бонус 3
    if completion < BONUS_3_THRESHOLD_LOW:
        bonus_3 = -BONUS_3_PENALTY_RATE * (plan - kpl)
    elif completion < BONUS_3_THRESHOLD_HIGH:
        bonus_3 = Decimal(0)
    else:
        bonus_3 = BONUS_3_OVER_RATE * bonus_1

    return bonus_1, bonus_2, bonus_3