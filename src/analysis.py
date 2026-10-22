"""Analysis: do heat, travel and age change when players are substituted and how teams finish games?

Writes reports/analysis.md and figures to reports/figures/.
"""
import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from lifelines import CoxPHFitter, KaplanMeierFitter  # noqa: E402

from .common import DB_PATH, REPORTS_DIR  # noqa: E402

WARM_C = 21  # heat threshold used in the UCL tracking study (Cardiff Met, 2024)
FIG_DIR = REPORTS_DIR / "figures"

COVARIATES = {
    "temp_per_5c": "Температура, +5°C",
    "age_per_5y": "Возраст, +5 лет (от 27)",
    "temp_x_age": "Температура × возраст",
    "pos_DEF": "Защитник (vs нападающий)",
    "pos_MID": "Полузащитник (vs нападающий)",
    "is_home": "Дома",
    "travel_per_1000km": "Перелёт, +1000 км",
    "tz_shift_abs": "Смена часового пояса, |ч|",
    "short_rest": "Отдых < 4 дней",
    "goal_diff_60": "Разница мячей на 60'",
    "humidity_per_10": "Влажность, +10 п.п.",
}
# Heat in the RPL is almost entirely July–September, i.e. the start of the season (rotation,
# pre-season fitness, new signings). Calendar-month fixed effects (not a linear trend: the season
# has a winter break) keep "heat" from meaning "early season". Reference month: October.
MONTHS = {3: "мар (+фев)", 4: "апр", 5: "май", 7: "июл", 8: "авг", 9: "сен", 11: "ноя", 12: "дек"}
COVARIATES.update({f"month_{m}": f"Месяц: {name} (vs окт)" for m, name in MONTHS.items()})
KEY_FACTORS = [
    "temp_per_5c", "age_per_5y", "temp_x_age", "travel_per_1000km", "tz_shift_abs", "short_rest",
    "humidity_per_10", "is_home", "goal_diff_60", "pos_DEF", "pos_MID",
]


def _starters(con):
    df = con.execute("""
        SELECT * FROM mart.starter_exposure
        WHERE position <> 'GK' AND temperature_c IS NOT NULL AND age_years IS NOT NULL
          AND rest_days IS NOT NULL
    """).df()
    df["temp_per_5c"] = (df["temperature_c"] - 15) / 5
    df["age_per_5y"] = (df["age_years"] - 27) / 5
    df["temp_x_age"] = df["temp_per_5c"] * df["age_per_5y"]
    df["pos_DEF"] = (df["position"] == "DEF").astype(int)
    df["pos_MID"] = (df["position"] == "MID").astype(int)
    df["is_home"] = df["is_home"].astype(int)
    df["travel_per_1000km"] = df["travel_km"].fillna(0) / 1000
    df["tz_shift_abs"] = df["tz_shift_h"].abs().fillna(0)
    df["short_rest"] = (df["rest_days"] < 4).astype(int)
    df["goal_diff_60"] = df["goal_diff_60"].clip(-2, 2)
    df["humidity_per_10"] = (df["humidity_pct"] - df["humidity_pct"].mean()) / 10
    month = df["kickoff_utc"].dt.month.replace({2: 3})  # 12 February matches folded into March
    for m in MONTHS:
        df[f"month_{m}"] = (month == m).astype(int)
    df["event"] = df["subbed_off"].astype(int)
    return df


def _cox(df, duration_col="duration_min", event_col="event"):
    cph = CoxPHFitter(penalizer=0.01)
    cph.fit(df[[*COVARIATES, duration_col, event_col, "match_id"]], duration_col=duration_col,
            event_col=event_col, cluster_col="match_id")
    s = cph.summary.loc[KEY_FACTORS]  # month fixed effects are controls, not reported
    out = pd.DataFrame({
        "Фактор": [COVARIATES[i] for i in s.index],
        "HR": s["exp(coef)"].round(3),
        "95% ДИ": [f"{lo:.3f}–{hi:.3f}" for lo, hi in zip(s["exp(coef) lower 95%"], s["exp(coef) upper 95%"])],
        "p": s["p"].map(lambda p: f"{p:.3f}" if p >= 0.001 else "<0.001"),
    })
    return cph, out


def _negative_control(df):
    """Exposure truncated at 30': early substitutions are mostly injuries/tactics, not heat fatigue.
    A strong temperature effect here would mean the main model picks up something other than fatigue."""
    nc = df.copy()
    nc["event"] = ((nc["event"] == 1) & (nc["duration_min"] <= 30)).astype(int)
    nc["duration_min"] = nc["duration_min"].clip(upper=30)
    return _cox(nc)[1]


INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
COOL, WARM = "#2a78d6", "#eb6834"  # categorical slots 1-2 of the reference palette


def _km_plot(df):
    """Two panels (age groups), each comparing cool vs warm matches.

    Plotted as the cumulative share already substituted (1 - KM survival) on a 0-based axis:
    "how many were taken off by minute X" reads more naturally than survival, and a 0 baseline
    keeps a 3-point gap from looking dramatic.
    """
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 5), sharey=True)
    panels = [("До 30 лет", df.age_years < 30), ("30 лет и старше", df.age_years >= 30)]
    conditions = [("прохладно, < 21°C", df.temperature_c < WARM_C, COOL, "-"),
                  ("жара, ≥ 21°C", df.temperature_c >= WARM_C, WARM, "--")]
    minutes = np.arange(0, 91)

    for ax, (title, age_mask) in zip(axes, panels):
        ends, sizes = [], []
        for label, temp_mask, color, style in conditions:
            sub = df[age_mask & temp_mask]
            km = KaplanMeierFitter().fit(sub.duration_min, sub.event)
            share = (1 - km.survival_function_at_times(minutes).to_numpy()) * 100
            ax.step(minutes, share, where="post", color=color, linestyle=style, linewidth=2)
            ax.plot([90], [share[-1]], marker="o", markersize=6, color=color, clip_on=False, zorder=3)
            ends.append([share[-1], share[-1], f"{label}\n{share[-1]:.0f}% заменены", color])
            sizes.append(f"{len(sub):,}".replace(",", " "))
        # Direct labels at the line ends; only the text is nudged apart, the dots stay on the data.
        ends.sort()
        if ends[1][1] - ends[0][1] < 7:
            mid = (ends[0][1] + ends[1][1]) / 2
            ends[0][1], ends[1][1] = mid - 3.5, mid + 3.5
        for y_data, y_text, text, color in ends:
            ax.annotate(text, xy=(90, y_data), xytext=(92, y_text), textcoords="data",
                        va="center", fontsize=9, color=INK_2, annotation_clip=False)
        ax.axvspan(45, 46, color=GRID, zorder=0)
        ax.text(45.5, 49, "перерыв", ha="center", va="top", fontsize=8, color=MUTED)
        ax.set_title(title, loc="left", fontsize=12, color=INK, pad=22)
        ax.text(0, 1.02, f"выборка: {sizes[0]} выходов в старте в прохладу, {sizes[1]} в жару",
                transform=ax.transAxes, fontsize=8.5, color=MUTED, va="bottom")
        ax.set_xlim(0, 90)
        ax.set_ylim(0, 50)
        ax.set_xticks([0, 15, 30, 45, 60, 75, 90])
        ax.set_xlabel("Минута матча", color=INK_2)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.tick_params(colors=MUTED)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#c3c2b7")
    axes[0].set_ylabel("Игроков старта уже заменено, %", color=INK_2)
    fig.suptitle("В жару игроков меняют чуть чаще — одинаково у молодых и возрастных",
                 x=0.06, ha="left", fontsize=14, color=INK, fontweight="bold")
    fig.text(0.06, 0.895, "Полевые игроки стартового состава, РПЛ 2023/24–2025/26. "
             "Без поправки на месяц: жаркие матчи — в основном июль–август.",
             ha="left", fontsize=9.5, color=INK_2)
    fig.subplots_adjust(left=0.06, right=0.86, top=0.76, bottom=0.12, wspace=0.45)
    fig.savefig(FIG_DIR / "km_age_temperature.png", dpi=150, facecolor="white")
    plt.close(fig)


def _forest_plot(table):
    fig, ax = plt.subplots(figsize=(8, 5))
    t = table.iloc[::-1]
    lo = t["95% ДИ"].str.split("–").str[0].astype(float)
    hi = t["95% ДИ"].str.split("–").str[1].astype(float)
    y = np.arange(len(t))
    ax.errorbar(t["HR"], y, xerr=[t["HR"] - lo, hi - t["HR"]], fmt="o", color="#333")
    ax.axvline(1, color="#999", linestyle="--")
    ax.set_yticks(y, t["Фактор"])
    ax.set_xlabel("Hazard ratio замены (> 1 — меняют раньше)")
    ax.set_title("Модель Кокса: что ускоряет замену игрока старта")
    fig.tight_layout()
    fig.savefig(FIG_DIR / "cox_forest.png", dpi=150)
    plt.close(fig)


def _team_tables(con):
    tm = con.execute("""
        SELECT *,
               CASE WHEN temperature_c < 10 THEN '1) < 10°C'
                    WHEN temperature_c < 21 THEN '2) 10–21°C'
                    ELSE '3) ≥ 21°C' END AS temp_band,
               CASE WHEN goal_diff_60 = 0 THEN 'ничья на 60''' ELSE 'не ничья на 60''' END AS state_60
        FROM mart.team_match_late
        WHERE temperature_c IS NOT NULL
    """).df()

    def agg(g):
        late = g.xg_75_end.sum() / (15 * len(g))   # xG per minute, 75'-end (≈15 min + added time)
        early = g.xg_0_60.sum() / (60 * len(g))
        return pd.Series({
            "командо-матчей": len(g),
            "xG/мин 0–60'": round(early, 4),
            "xG/мин 75'+": round(late, 4),
            "отношение поздн./ран.": round(late / early, 2) if early else np.nan,
            "1-я замена, мин (ср.)": round(g.first_sub_minute.mean(), 1),
            "замен к 60' (ср.)": round(g.subs_by_60.mean(), 2),
        })

    b = tm[tm.has_understat].groupby(["state_60", "temp_band"]).apply(agg, include_groups=False).reset_index()
    c = tm.groupby("temp_band").apply(agg, include_groups=False).reset_index()[
        ["temp_band", "командо-матчей", "1-я замена, мин (ср.)", "замен к 60' (ср.)"]]
    return b, c, tm


def _md(df):
    cols = list(df.columns)
    return ("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
            + "".join("| " + " | ".join(str(v) for v in r) + " |\n" for r in df.itertuples(index=False)))


def run():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = _starters(con)
    cph, main = _cox(df)
    nc = _negative_control(df)
    b, c, tm = _team_tables(con)
    con.close()

    _km_plot(df)
    _forest_plot(main)

    warm = df[df.temperature_c >= WARM_C]
    n_rows = f"{len(df):,}".replace(",", " ")
    desc = (
        f"- Игроков старта (полевые): **{n_rows}** записей в **{df.match_id.nunique()}** матчах "
        f"(первый матч команды в сезоне исключён: для него нет дней отдыха); "
        f"заменены до 90': **{df.event.mean():.0%}**.\n"
        f"- Температура в игровом окне: медиана {df.temperature_c.median():.1f}°C, "
        f"от {df.temperature_c.min():.1f} до {df.temperature_c.max():.1f}°C. "
        f"Матчей при ≥ {WARM_C}°C: {warm.match_id.nunique()}; игроков 30+ в них: {(warm.age_years >= 30).sum()}.\n"
        f"- Индекс согласованности модели (C-index): {cph.concordance_index_:.3f}.\n"
    )

    report = f"""# Анализ: жара, перелёты, возраст и концовка матча

Сгенерирован `python -m src.analysis`. Данные: РПЛ 2023/24–2025/26, витрины `mart.*`.

## Выборка

{desc}
## Метрика A: время до замены (модель Кокса)

Единица — игрок стартового состава (без вратарей) в матче. Событие — замена до 90'.
Замены в добавленное время и удаления — цензурирование. Стандартные ошибки кластеризованы по матчу.
HR > 1 — игрока меняют раньше.

{_md(main)}
![Forest plot](figures/cox_forest.png)

![Kaplan–Meier](figures/km_age_temperature.png)

### Отрицательный контроль: только первые 30 минут

Ранние замены — в основном травмы и тактика. Если бы температура сильно влияла и здесь,
основная модель ловила бы не усталость, а что-то другое.

{_md(nc[nc["Фактор"].isin([COVARIATES["temp_per_5c"], COVARIATES["age_per_5y"], COVARIATES["temp_x_age"]])])}
## Метрика B: атака в концовке vs начало матча (Understat xG)

Поздний отрезок — 75' и до конца (знаменатель 15 минут, добавленное время не учитывается,
поэтому отношение завышено одинаково для всех групп). Разбивка по счёту на 60', чтобы отделить
эффект усталости от эффекта счёта.

{_md(b)}
## Метрика C: как тренеры распоряжаются заменами

{_md(c)}
"""
    (REPORTS_DIR / "analysis.md").write_text(report, encoding="utf-8")
    print(main.to_string(index=False))
    print(nc.to_string(index=False))
    print(b.to_string(index=False))
    print(c.to_string(index=False))


if __name__ == "__main__":
    run()
