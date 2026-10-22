-- Staging: typed, renamed, normalised copies of raw tables. One row = one source entity.
CREATE SCHEMA IF NOT EXISTS stg;

CREATE OR REPLACE TABLE stg.season AS
SELECT * FROM (VALUES (719, 2023, '2023/24'), (721, 2024, '2024/25'), (722, 2025, '2025/26'))
    AS t(tournament, season, season_label);

-- RPL labels kick-off as "2025-07-18UTC20:30:00" but the clock is Moscow time (UTC+3, no DST since 2014).
-- Verified against Understat (true UTC); see DQ check `kickoff_vs_understat`.
CREATE OR REPLACE TABLE stg.rpl_match AS
SELECT
    m.match_id,
    s.season,
    s.season_label,
    m.stage_id,
    m.club_h,
    m.club_a,
    m.goal_h,
    m.goal_a,
    strptime(replace(m.date_raw, 'UTC', ' '), '%Y-%m-%d %H:%M:%S')                         AS kickoff_msk,
    strptime(replace(m.date_raw, 'UTC', ' '), '%Y-%m-%d %H:%M:%S') - INTERVAL 3 HOUR       AS kickoff_utc,
    m.city,
    m.stadium,
    m.attendance
FROM raw.rpl_matches m
JOIN stg.season s USING (tournament);

CREATE OR REPLACE TABLE stg.rpl_club AS
SELECT DISTINCT club_id, name AS club_name, city AS club_city
FROM raw.rpl_clubs;

CREATE OR REPLACE TABLE stg.rpl_player AS
SELECT
    id                                                      AS player_id,
    trim(coalesce(nullif(public_lastname, ''), lastname))   AS last_name,
    trim(coalesce(nullif(public_name, ''), name))           AS first_name,
    nullif(nickname, '')                                    AS nickname,
    try_cast(substr(birthday, 1, 10) AS DATE)               AS birth_date,
    nullif(height, 0)                                       AS height_cm,
    nullif(weight, 0)                                       AS weight_kg
FROM raw.rpl_players;

CREATE OR REPLACE TABLE stg.rpl_lineup AS
SELECT
    match_id,
    club_id,
    is_home,
    player_id,
    is_starter,
    CASE amplua WHEN 0 THEN 'GK' WHEN 1 THEN 'DEF' WHEN 2 THEN 'MID' WHEN 3 THEN 'FWD' END AS position
FROM raw.rpl_lineups;

-- Event codes are undocumented; mapping inferred and verified: goals reconcile with the final score
-- in every match (DQ check `goals_vs_score`). Substitution: player1 leaves, player2 enters.
CREATE OR REPLACE TABLE stg.rpl_event AS
SELECT
    match_id,
    event_id,
    CASE type
        WHEN 1  THEN 'goal'
        WHEN 10 THEN 'goal_penalty'
        WHEN 11 THEN 'own_goal'
        WHEN 2  THEN 'substitution'
        WHEN 3  THEN 'yellow'
        WHEN 4  THEN CASE WHEN second_yellow THEN 'second_yellow' ELSE 'red' END
        ELSE 'other_' || type
    END                                     AS event_kind,
    minute,
    coalesce(extra, 0)                      AS added_minute,
    minute + coalesce(extra, 0)             AS match_minute,
    player1_id,
    player2_id,
    club_id,
    CAST(custom_html AS VARCHAR)            AS custom_html  -- all-NULL column arrives untyped from pandas
FROM raw.rpl_events;

CREATE OR REPLACE TABLE stg.us_match AS
SELECT
    us_match_id,
    season,
    h_id                                AS us_team_h,
    h_title                             AS us_team_h_name,
    a_id                                AS us_team_a,
    a_title                             AS us_team_a_name,
    CAST(h_goals AS INT)                AS goal_h,
    CAST(a_goals AS INT)                AS goal_a,
    CAST(h_xg AS DOUBLE)                AS xg_h,
    CAST(a_xg AS DOUBLE)                AS xg_a,
    CAST(datetime_raw AS TIMESTAMP)     AS kickoff_utc
FROM raw.us_matches;

CREATE OR REPLACE TABLE stg.us_shot AS
SELECT
    us_match_id,
    CAST(shot_id AS BIGINT)     AS shot_id,
    h_a = 'h'                   AS is_home,
    CAST(minute AS INT)         AS minute,
    result,
    CAST(xg AS DOUBLE)          AS xg,
    situation
FROM raw.us_shots;

CREATE OR REPLACE TABLE stg.us_roster AS
SELECT
    us_match_id,
    h_a = 'h'                   AS is_home,
    CAST(us_player_id AS INT)   AS us_player_id,
    player                      AS player_name,
    CAST(us_team_id AS INT)     AS us_team_id,
    CAST(minutes AS INT)        AS minutes,
    position
FROM raw.us_rosters;

-- Venue reference data: coordinates and UTC offset, maintained by hand in seeds/venues.csv.
CREATE OR REPLACE TABLE stg.venue AS
SELECT venue_key, stadium, city, CAST(lat AS DOUBLE) AS lat, CAST(lon AS DOUBLE) AS lon,
       CAST(utc_offset AS INT) AS utc_offset
FROM read_csv('seeds/venues.csv', header = true, all_varchar = true);

CREATE OR REPLACE TABLE stg.weather_hourly AS
SELECT venue AS venue_key, season, CAST(time_utc AS TIMESTAMP) AS time_utc,
       temperature_c, apparent_temperature_c, humidity_pct, precipitation_mm, wind_kmh
FROM raw.weather_hourly;
