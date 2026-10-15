-- cnipa_service MySQL schema
-- Host: 192.168.100.229 (或 localhost) / DB: cnipa_db / User: cnipauser

CREATE DATABASE IF NOT EXISTS cnipa_db
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE cnipa_db;

-- 任务主表
CREATE TABLE IF NOT EXISTS cnipa_task (
  task_id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'task_id',
  task_type       TINYINT UNSIGNED NOT NULL COMMENT '1=决定检索(+详情+附图) 2=仅摘要附图',
  query_key       CHAR(64) NOT NULL COMMENT '规范化查询条件哈希，用于去重',
  query_json      JSON NOT NULL COMMENT '原始查询参数',
  status          TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '1排队 2执行中 3成功有结果 4成功无命中 5失败',
  progress        VARCHAR(200) NULL COMMENT '可选进度说明',
  error_code      VARCHAR(64) NULL COMMENT '失败时错误码',
  error_message   VARCHAR(1000) NULL COMMENT '失败说明',
  result_json     JSON NULL COMMENT '完成时的契约结果（列表/详情等；附图见文件表）',
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  started_at      DATETIME(3) NULL,
  finished_at     DATETIME(3) NULL,
  PRIMARY KEY (task_id),
  KEY idx_query_key_status (query_key, status),
  KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='国知局异步查询任务';

-- 附图文件（一任务可多申请号）
CREATE TABLE IF NOT EXISTS cnipa_task_figure (
  figure_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  task_id         BIGINT UNSIGNED NOT NULL,
  application_no  VARCHAR(32) NOT NULL COMMENT '含点申请号，如 200610004173.6',
  has_figure      TINYINT UNSIGNED NOT NULL DEFAULT 1 COMMENT '1有图 0无图(负结果)',
  rel_path        VARCHAR(512) NULL COMMENT '相对 D:/cnipa_file，以/开头，如 /200610004173.6/figure.png；URL={域名}/cnipa_file{rel_path}',
  created_at      DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (figure_id),
  UNIQUE KEY uk_task_app (task_id, application_no),
  KEY idx_application_no (application_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='任务关联摘要附图';
