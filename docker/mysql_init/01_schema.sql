-- Gateway usage log schema.
-- Mounted into the MySQL container at /docker-entrypoint-initdb.d, so it
-- runs once on the first container start (when the data directory is empty).

CREATE DATABASE IF NOT EXISTS gateway_log
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE gateway_log;

-- One row per /v1/responses turn (streaming or non-streaming).
CREATE TABLE IF NOT EXISTS usage_turn (
  id                    BIGINT PRIMARY KEY AUTO_INCREMENT,
  ts                    DATETIME(3) NOT NULL,
  user                  VARCHAR(128) NOT NULL,
  session_id            VARCHAR(64)  NOT NULL,
  response_id           VARCHAR(128) NOT NULL,
  previous_response_id  VARCHAR(128) NULL,
  turn                  INT          NOT NULL,
  model                 VARCHAR(64)  NULL,
  backend               VARCHAR(32)  NULL,
  input_tokens          INT          NOT NULL DEFAULT 0,
  output_tokens         INT          NOT NULL DEFAULT 0,
  cache_read_tokens     INT          NOT NULL DEFAULT 0,
  cache_creation_tokens INT          NOT NULL DEFAULT 0,
  duration_ms           INT          NOT NULL DEFAULT 0,
  status                VARCHAR(32)  NOT NULL,
  error_code            VARCHAR(64)  NULL,
  KEY idx_user_ts   (user, ts),
  KEY idx_session   (session_id),
  KEY idx_response  (response_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- One row per distinct tool name per turn (aggregated: count / errors / total_ms).
CREATE TABLE IF NOT EXISTS usage_tool (
  id                BIGINT PRIMARY KEY AUTO_INCREMENT,
  turn_id           BIGINT NOT NULL,
  tool_name         VARCHAR(128) NOT NULL,
  call_count        INT NOT NULL,
  error_count       INT NOT NULL DEFAULT 0,
  total_duration_ms INT NOT NULL DEFAULT 0,
  KEY idx_turn (turn_id),
  KEY idx_tool (tool_name),
  CONSTRAINT fk_usage_tool_turn
    FOREIGN KEY (turn_id) REFERENCES usage_turn(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
