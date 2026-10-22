-- Cross-source id mapping. Teams are matched by the fixtures they played (same UTC date, same score),
-- never by name: Understat spells the two RPL "Dynamo" clubs differently ("Dinamo Moscow" vs
-- "Dynamo Makhachkala"), so string normalisation is a way to merge two different clubs.
CREATE SCHEMA IF NOT EXISTS xref;

CREATE OR REPLACE TABLE xref.team_votes AS
WITH candidate_pairs AS (
    SELECT r.season, r.club_h AS rpl_club_id, u.us_team_h AS us_team_id
    FROM stg.rpl_match r
    JOIN stg.us_match u
      ON u.season = r.season
     AND CAST(u.kickoff_utc AS DATE) = CAST(r.kickoff_utc AS DATE)
     AND u.goal_h = r.goal_h AND u.goal_a = r.goal_a
    UNION ALL
    SELECT r.season, r.club_a, u.us_team_a
    FROM stg.rpl_match r
    JOIN stg.us_match u
      ON u.season = r.season
     AND CAST(u.kickoff_utc AS DATE) = CAST(r.kickoff_utc AS DATE)
     AND u.goal_h = r.goal_h AND u.goal_a = r.goal_a
)
SELECT rpl_club_id, us_team_id, count(*) AS votes
FROM candidate_pairs
GROUP BY ALL;

-- Winner per RPL club. Losing candidates are coincidences (another fixture on the same date with the
-- same score), so confidence = the winner explains every fixture the club played, with a wide margin.
CREATE OR REPLACE TABLE xref.team AS
WITH played AS (
    SELECT club_id, count(*) AS matches_played
    FROM (SELECT club_h AS club_id FROM stg.rpl_match UNION ALL SELECT club_a FROM stg.rpl_match)
    GROUP BY 1
), ranked AS (
    SELECT *,
           row_number() OVER (PARTITION BY rpl_club_id ORDER BY votes DESC) AS rnk,
           lead(votes) OVER (PARTITION BY rpl_club_id ORDER BY votes DESC)  AS runner_up_votes
    FROM xref.team_votes
)
SELECT
    r.rpl_club_id,
    c.club_name,
    r.us_team_id,
    (SELECT any_value(us_team_h_name) FROM stg.us_match WHERE us_team_h = r.us_team_id) AS us_team_name,
    r.votes,
    p.matches_played,
    coalesce(r.runner_up_votes, 0) AS runner_up_votes
FROM ranked r
JOIN stg.rpl_club c ON c.club_id = r.rpl_club_id
JOIN played p ON p.club_id = r.rpl_club_id
WHERE r.rnk = 1;

-- Match mapping: same season, same home and away team via xref.team, kick-off within 36 hours.
CREATE OR REPLACE TABLE xref.match AS
SELECT
    r.match_id,
    u.us_match_id,
    date_diff('minute', u.kickoff_utc, r.kickoff_utc) AS kickoff_diff_min
FROM stg.rpl_match r
JOIN xref.team th ON th.rpl_club_id = r.club_h
JOIN xref.team ta ON ta.rpl_club_id = r.club_a
JOIN stg.us_match u
  ON u.season = r.season
 AND u.us_team_h = th.us_team_id
 AND u.us_team_a = ta.us_team_id
 AND abs(date_diff('hour', u.kickoff_utc, r.kickoff_utc)) <= 36;
