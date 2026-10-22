"""Data quality checks. Each check is a SQL query returning the offending rows.

`error` checks block the analysis (the pipeline exits non-zero); `warn` checks are reported
and explained. The result is written to reports/dq_report.md.
"""
import sys
from dataclasses import dataclass

import duckdb

from .common import DB_PATH, REPORTS_DIR


@dataclass
class Check:
    name: str
    severity: str  # "error" | "warn"
    description: str
    sql: str


CHECKS = [
    Check("match_id_unique", "error", "Один матч РПЛ — одна строка.",
          "SELECT match_id, count(*) n FROM stg.rpl_match GROUP BY 1 HAVING n > 1"),
    Check("lineup_unique", "error", "Игрок не может быть в протоколе матча дважды.",
          "SELECT match_id, player_id, count(*) n FROM stg.rpl_lineup GROUP BY ALL HAVING n > 1"),
    Check("eleven_starters", "warn", "У каждой команды в каждом матче ровно 11 игроков в старте.",
          """SELECT tm.match_id, tm.club_id, count(l.player_id) starters
             FROM mart.team_match tm
             LEFT JOIN stg.rpl_lineup l ON l.match_id = tm.match_id AND l.club_id = tm.club_id AND l.is_starter
             GROUP BY ALL HAVING starters <> 11"""),
    Check("goals_vs_score", "error",
          "Голы из событий (с учётом автоголов) совпадают с итоговым счётом. Подтверждает расшифровку кодов событий.",
          """SELECT m.match_id, m.goal_h, m.goal_a,
                    count(*) FILTER (g.scoring_club_id = m.club_h) ev_h,
                    count(*) FILTER (g.scoring_club_id = m.club_a) ev_a
             FROM stg.rpl_match m LEFT JOIN mart.goal g USING (match_id)
             GROUP BY ALL HAVING ev_h <> m.goal_h OR ev_a <> m.goal_a"""),
    Check("max_five_subs", "warn",
          "Не больше 5 замен на команду. Шестая допустима только как дополнительная замена при сотрясении — сверить вручную.",
          "SELECT match_id, club_id, count(*) n FROM stg.rpl_event WHERE event_kind='substitution' GROUP BY ALL HAVING n > 5"),
    Check("sub_off_was_on_pitch", "warn",
          "Заменённый игрок был в старте или вышел на замену раньше.",
          """SELECT e.match_id, e.player1_id, e.match_minute
             FROM stg.rpl_event e
             WHERE e.event_kind = 'substitution'
               AND NOT EXISTS (SELECT 1 FROM stg.rpl_lineup l
                               WHERE l.match_id = e.match_id AND l.player_id = e.player1_id AND l.is_starter)
               AND NOT EXISTS (SELECT 1 FROM stg.rpl_event e2
                               WHERE e2.match_id = e.match_id AND e2.event_kind = 'substitution'
                                 AND e2.player2_id = e.player1_id AND e2.match_minute <= e.match_minute)"""),
    Check("player_birth_date", "error", "У каждого игрока стартового состава есть дата рождения, возраст 15–45 лет.",
          """SELECT match_id, player_id, age_years FROM mart.starter_exposure
             WHERE age_years IS NULL OR age_years NOT BETWEEN 15 AND 45"""),
    Check("venue_has_coords", "error", "У каждого стадиона матча есть координаты и часовой пояс (seeds/venues.csv).",
          """SELECT DISTINCT m.stadium, m.city FROM stg.rpl_match m
             LEFT JOIN stg.venue v ON v.stadium = m.stadium WHERE v.venue_key IS NULL"""),
    Check("weather_coverage", "error", "Для каждого матча есть погода на все 3 часа игрового окна.",
          "SELECT match_id, venue_key, weather_hours FROM mart.match_weather WHERE weather_hours <> 3"),
    Check("team_xref_one_to_one", "error",
          "Соответствие команд РПЛ ↔ Understat взаимно однозначно (ловит склейку двух «Динамо»).",
          """SELECT us_team_id, list(club_name) clubs FROM xref.team GROUP BY 1 HAVING count(*) > 1"""),
    Check("team_xref_confident", "error",
          "Сопоставление команды подтверждено всеми её матчами, а случайные совпадения (дата + счёт) — не больше 25%.",
          """SELECT club_name, us_team_name, votes, matches_played, runner_up_votes FROM xref.team
             WHERE votes <> matches_played OR runner_up_votes > 0.25 * votes"""),
    Check("match_xref_coverage", "warn", "Каждый матч РПЛ найден в Understat.",
          """SELECT m.match_id, m.season, m.kickoff_msk, m.club_h, m.club_a FROM stg.rpl_match m
             LEFT JOIN xref.match x USING (match_id) WHERE x.match_id IS NULL"""),
    Check("kickoff_vs_understat", "warn",
          "После перевода МСК→UTC время начала совпадает с Understat (±15 мин). Расхождения в ±60 мин скапливаются "
          "у дат перевода часов в Европе: Understat, похоже, хранит время не в чистом UTC. Эталон — РПЛ.",
          """SELECT x.match_id, x.us_match_id, m.kickoff_msk, x.kickoff_diff_min
             FROM xref.match x JOIN stg.rpl_match m USING (match_id)
             WHERE abs(x.kickoff_diff_min) > 15 ORDER BY m.kickoff_msk"""),
    Check("understat_roster_duplicates", "warn", "Игрок встречается в составе Understat на матч один раз.",
          """SELECT us_match_id, us_player_id, player_name, count(*) n FROM stg.us_roster
             GROUP BY ALL HAVING n > 1"""),
    Check("score_vs_understat", "error", "Счёт в РПЛ и Understat совпадает.",
          """SELECT m.match_id, m.goal_h, m.goal_a, u.goal_h us_h, u.goal_a us_a
             FROM xref.match x JOIN stg.rpl_match m USING (match_id) JOIN stg.us_match u USING (us_match_id)
             WHERE m.goal_h <> u.goal_h OR m.goal_a <> u.goal_a"""),
    Check("players_used_vs_understat", "warn",
          "Число сыгравших игроков команды в РПЛ (старт + вышедшие на замену) совпадает с Understat.",
          """WITH rpl AS (
                 SELECT l.match_id, l.is_home, count(*) n FROM stg.rpl_lineup l
                 WHERE l.is_starter OR EXISTS (SELECT 1 FROM stg.rpl_event e WHERE e.match_id = l.match_id
                                               AND e.event_kind = 'substitution' AND e.player2_id = l.player_id)
                 GROUP BY ALL),
             us AS (SELECT x.match_id, r.is_home, count(*) n FROM stg.us_roster r
                    JOIN xref.match x USING (us_match_id) GROUP BY ALL)
             SELECT rpl.match_id, rpl.is_home, rpl.n rpl_players, us.n us_players
             FROM rpl JOIN us USING (match_id, is_home) WHERE rpl.n <> us.n"""),
    Check("technical_result", "warn", "Матчи с техническим результатом (счёт в протоколе ≠ игре на поле).",
          "SELECT match_id, left(regexp_replace(custom_html, '<[^>]+>', ' ', 'g'), 120) note FROM stg.rpl_event WHERE custom_html IS NOT NULL"),
    Check("neutral_or_moved_venue", "warn",
          "Матчи не на основном домашнем стадионе хозяев (перенос, нейтральное поле).",
          """SELECT tm.match_id, tm.season, c.club_name, tm.venue_key, tm.home_venue_key
             FROM mart.team_match tm JOIN stg.rpl_club c ON c.club_id = tm.club_id
             WHERE tm.is_home AND tm.venue_key <> tm.home_venue_key"""),
    Check("shared_home_venue", "warn", "Несколько клубов делят один домашний стадион в сезоне.",
          """SELECT season, venue_key, list(c.club_name) clubs FROM mart.club_home_venue h
             JOIN stg.rpl_club c USING (club_id) GROUP BY ALL HAVING count(*) > 1"""),
]


def _md_table(df, limit=10):
    if df.empty:
        return ""
    shown = df.head(limit)
    head = "| " + " | ".join(map(str, shown.columns)) + " |\n|" + "---|" * len(shown.columns) + "\n"
    body = "".join("| " + " | ".join(str(v) for v in row) + " |\n" for row in shown.itertuples(index=False))
    more = f"\n…и ещё {len(df) - limit}\n" if len(df) > limit else ""
    return head + body + more


def run(log=print) -> bool:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    lines, failed_errors = [], 0
    summary = []
    for c in CHECKS:
        df = con.execute(c.sql).df()
        status = "✅" if df.empty else ("❌" if c.severity == "error" else "⚠️")
        failed_errors += (not df.empty) and c.severity == "error"
        summary.append(f"| {status} | `{c.name}` | {c.severity} | {len(df)} | {c.description} |")
        if not df.empty:
            lines.append(f"### {status} `{c.name}` — {len(df)}\n\n{c.description}\n\n{_md_table(df)}")
        log(f"{status} {c.name}: {len(df)}")

    # The headline finding: what the raw RPL timestamp would have cost if trusted as UTC.
    tz = con.execute("""
        SELECT count(*) n,
               round(avg(kickoff_diff_min), 1) avg_diff_after_fix,
               round(avg(kickoff_diff_min + 180), 1) avg_diff_if_label_trusted
        FROM xref.match""").fetchone()
    temp = con.execute("""
        WITH t AS (
            SELECT m.match_id,
                   avg(w.temperature_c) FILTER (w.time_utc = date_trunc('hour', m.kickoff_utc)) correct,
                   avg(w.temperature_c) FILTER (w.time_utc = date_trunc('hour', m.kickoff_msk)) wrong
            FROM stg.rpl_match m JOIN stg.venue v ON v.stadium = m.stadium
            JOIN stg.weather_hourly w ON w.venue_key = v.venue_key AND w.season = m.season
            GROUP BY 1)
        SELECT round(avg(abs(correct - wrong)), 1), round(max(abs(correct - wrong)), 1) FROM t""").fetchone()
    con.close()

    REPORTS_DIR.mkdir(exist_ok=True)
    report = (
        "# Отчёт о качестве данных\n\n"
        "Сгенерирован `python -m src.pipeline`. ❌ — ошибка (блокирует анализ), ⚠️ — предупреждение, "
        "ожидаемое и объяснённое в README.\n\n"
        "## Главная находка: метка «UTC» в API РПЛ\n\n"
        f"API РПЛ отдаёт время начала как `2025-07-18UTC20:30:00`, но это московское время. "
        f"На {tz[0]} матчах, сопоставленных с Understat, после перевода МСК→UTC среднее расхождение "
        f"{tz[1]} мин; если поверить метке, было бы {tz[2]} мин (3 часа). "
        f"Погода, взятая по неверному часу, отличалась бы в среднем на {temp[0]}°C, максимум на {temp[1]}°C.\n\n"
        "## Сводка\n\n| | Проверка | Уровень | Строк | Что проверяем |\n|---|---|---|---|---|\n"
        + "\n".join(summary) + "\n\n## Детали\n\n" + "\n".join(lines)
    )
    (REPORTS_DIR / "dq_report.md").write_text(report, encoding="utf-8")
    return failed_errors == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
