-- Marts: analysis-ready tables.
CREATE SCHEMA IF NOT EXISTS mart;

CREATE OR REPLACE MACRO haversine_km(lat1, lon1, lat2, lon2) AS
    2 * 6371 * asin(sqrt(
        pow(sin(radians(lat2 - lat1) / 2), 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * pow(sin(radians(lon2 - lon1) / 2), 2)));

-- Weather over the playing window: the kick-off hour and the two following hours.
CREATE OR REPLACE TABLE mart.match_weather AS
SELECT
    m.match_id,
    v.venue_key,
    avg(w.temperature_c)            AS temperature_c,
    avg(w.apparent_temperature_c)   AS apparent_temperature_c,
    avg(w.humidity_pct)             AS humidity_pct,
    sum(w.precipitation_mm)         AS precipitation_mm,
    avg(w.wind_kmh)                 AS wind_kmh,
    count(w.time_utc)               AS weather_hours
FROM stg.rpl_match m
LEFT JOIN stg.venue v ON v.stadium = m.stadium
LEFT JOIN stg.weather_hourly w
  ON w.venue_key = v.venue_key
 AND w.season = m.season
 AND w.time_utc BETWEEN date_trunc('hour', m.kickoff_utc) AND date_trunc('hour', m.kickoff_utc) + INTERVAL 2 HOUR
GROUP BY ALL;

-- A club's home venue = the venue where it played most of its home matches that season
-- (the club-profile endpoint only knows the *current* stadium, which breaks history).
CREATE OR REPLACE TABLE mart.club_home_venue AS
WITH counts AS (
    SELECT m.season, m.club_h AS club_id, v.venue_key, count(*) AS home_matches
    FROM stg.rpl_match m
    JOIN stg.venue v ON v.stadium = m.stadium
    GROUP BY ALL
)
SELECT * FROM counts
QUALIFY row_number() OVER (PARTITION BY season, club_id ORDER BY home_matches DESC) = 1;

-- Goals with the team that benefits (own goals count for the opponent).
CREATE OR REPLACE TABLE mart.goal AS
SELECT
    e.match_id,
    e.match_minute,
    CASE WHEN e.event_kind = 'own_goal'
         THEN CASE WHEN e.club_id = m.club_h THEN m.club_a ELSE m.club_h END
         ELSE e.club_id END AS scoring_club_id
FROM stg.rpl_event e
JOIN stg.rpl_match m USING (match_id)
WHERE e.event_kind IN ('goal', 'goal_penalty', 'own_goal');

-- One row per team per match: context the team played in.
CREATE OR REPLACE TABLE mart.team_match AS
WITH sides AS (
    SELECT match_id, season, kickoff_utc, stadium, club_h AS club_id, club_a AS opponent_id, TRUE AS is_home
    FROM stg.rpl_match
    UNION ALL
    SELECT match_id, season, kickoff_utc, stadium, club_a, club_h, FALSE
    FROM stg.rpl_match
)
SELECT
    s.match_id,
    s.season,
    s.kickoff_utc,
    s.club_id,
    s.opponent_id,
    s.is_home,
    v.venue_key,
    hv.venue_key                                                        AS home_venue_key,
    round(haversine_km(h.lat, h.lon, v.lat, v.lon))                     AS travel_km,
    v.utc_offset - h.utc_offset                                         AS tz_shift_h,
    date_diff('hour',
        lag(s.kickoff_utc) OVER (PARTITION BY s.season, s.club_id ORDER BY s.kickoff_utc),
        s.kickoff_utc) / 24.0                                           AS rest_days,
    w.temperature_c,
    w.apparent_temperature_c,
    w.humidity_pct,
    w.precipitation_mm,
    w.wind_kmh,
    (SELECT count(*) FROM mart.goal g WHERE g.match_id = s.match_id AND g.scoring_club_id = s.club_id AND g.match_minute <= 60)
  - (SELECT count(*) FROM mart.goal g WHERE g.match_id = s.match_id AND g.scoring_club_id = s.opponent_id AND g.match_minute <= 60)
                                                                        AS goal_diff_60
FROM sides s
LEFT JOIN stg.venue v ON v.stadium = s.stadium
LEFT JOIN mart.club_home_venue hv ON hv.season = s.season AND hv.club_id = s.club_id
LEFT JOIN stg.venue h ON h.venue_key = hv.venue_key
LEFT JOIN mart.match_weather w ON w.match_id = s.match_id;

-- Metric A: one row per starting player; time on pitch until substituted (event) or censored.
-- Substitutions at 90'+ are stoppage-time game management, not fatigue: treated as censored at 90.
-- A red card ends exposure without a substitution: censored at the dismissal minute.
CREATE OR REPLACE TABLE mart.starter_exposure AS
WITH sub_off AS (
    SELECT match_id, player1_id AS player_id, min(match_minute) AS sub_minute
    FROM stg.rpl_event WHERE event_kind = 'substitution' GROUP BY ALL
), sent_off AS (
    SELECT match_id, player1_id AS player_id, min(match_minute) AS red_minute
    FROM stg.rpl_event WHERE event_kind IN ('red', 'second_yellow') GROUP BY ALL
)
SELECT
    l.match_id,
    l.player_id,
    l.club_id,
    l.position,
    date_diff('day', p.birth_date, CAST(tm.kickoff_utc AS DATE)) / 365.25  AS age_years,
    s.sub_minute,
    r.red_minute,
    CASE
        WHEN s.sub_minute < 90 AND (r.red_minute IS NULL OR s.sub_minute < r.red_minute) THEN s.sub_minute
        WHEN r.red_minute < 90 THEN r.red_minute
        ELSE 90
    END                                                                     AS duration_min,
    (s.sub_minute < 90 AND (r.red_minute IS NULL OR s.sub_minute < r.red_minute)) IS TRUE AS subbed_off,
    tm.* EXCLUDE (match_id, club_id)
FROM stg.rpl_lineup l
JOIN mart.team_match tm ON tm.match_id = l.match_id AND tm.club_id = l.club_id
LEFT JOIN stg.rpl_player p ON p.player_id = l.player_id
LEFT JOIN sub_off s ON s.match_id = l.match_id AND s.player_id = l.player_id
LEFT JOIN sent_off r ON r.match_id = l.match_id AND r.player_id = l.player_id
WHERE l.is_starter;

-- Metric B: attacking output early vs late, per team per match (Understat xG, matched via xref).
-- Metric C: first substitution minute and substitutions made by 60'.
CREATE OR REPLACE TABLE mart.team_match_late AS
WITH shots AS (
    SELECT x.match_id, sh.minute, sh.xg,
           CASE WHEN sh.is_home THEN m.club_h ELSE m.club_a END AS club_id
    FROM stg.us_shot sh
    JOIN xref.match x ON x.us_match_id = sh.us_match_id
    JOIN stg.rpl_match m ON m.match_id = x.match_id
), subs AS (
    SELECT match_id, club_id,
           min(match_minute)                    AS first_sub_minute,
           count(*) FILTER (match_minute <= 60) AS subs_by_60,
           count(*)                             AS subs_total
    FROM stg.rpl_event WHERE event_kind = 'substitution' GROUP BY ALL
)
SELECT
    tm.*,
    coalesce(sum(sh.xg) FILTER (sh.minute < 60), 0)   AS xg_0_60,
    coalesce(sum(sh.xg) FILTER (sh.minute >= 75), 0)  AS xg_75_end,
    count(sh.xg) FILTER (sh.minute < 60)              AS shots_0_60,
    count(sh.xg) FILTER (sh.minute >= 75)             AS shots_75_end,
    x.us_match_id IS NOT NULL                         AS has_understat,
    sb.first_sub_minute,
    coalesce(sb.subs_by_60, 0)                        AS subs_by_60,
    coalesce(sb.subs_total, 0)                        AS subs_total
FROM mart.team_match tm
LEFT JOIN xref.match x ON x.match_id = tm.match_id
LEFT JOIN shots sh ON sh.match_id = tm.match_id AND sh.club_id = tm.club_id
LEFT JOIN subs sb ON sb.match_id = tm.match_id AND sb.club_id = tm.club_id
GROUP BY ALL;
