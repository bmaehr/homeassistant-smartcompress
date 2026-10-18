import argparse
from collections import Counter
from datetime import datetime, time, timedelta
import fnmatch
import json
import logging
import sqlite3
import sys
import time as time_mod
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_CONFIG_CANDIDATES = [
    "config.postgres.json",
    "config.sqlite.json",
    "config.json",
]
DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "compression_rules.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ha_smartcompress")


def resolve_default_config_path():
    script_dir = Path(__file__).resolve().parent
    for candidate in DEFAULT_CONFIG_CANDIDATES:
        cand_path = script_dir / candidate
        if cand_path.is_file():
            return cand_path
    return script_dir / "config.json"


def parse_ha_configuration_yaml(yaml_path):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to parse configuration.yaml. Install via 'pip install pyyaml'.") from exc

    path = Path(yaml_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Home Assistant configuration file not found at: {path}")

    secrets_path = path.parent / "secrets.yaml"
    secrets = {}
    if secrets_path.is_file():
        try:
            with open(secrets_path, "r", encoding="utf-8") as sf:
                secrets = yaml.safe_load(sf) or {}
        except Exception as exc:
            logger.warning("Failed to load secrets.yaml: %s", exc)

    class SafeSecretLoader(yaml.SafeLoader):
        pass

    def secret_constructor(loader, node):
        sec_key = loader.construct_scalar(node)
        return secrets.get(sec_key, "")

    def ignore_constructor(loader, node):
        return None

    SafeSecretLoader.add_constructor("!secret", secret_constructor)
    SafeSecretLoader.add_constructor("!include", ignore_constructor)
    SafeSecretLoader.add_constructor("!include_dir_list", ignore_constructor)
    SafeSecretLoader.add_constructor("!include_dir_named", ignore_constructor)
    SafeSecretLoader.add_constructor("!include_dir_merge_list", ignore_constructor)
    SafeSecretLoader.add_constructor("!include_dir_merge_named", ignore_constructor)
    SafeSecretLoader.add_constructor(None, ignore_constructor)

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f, Loader=SafeSecretLoader) or {}

    recorder_conf = data.get("recorder", {}) if isinstance(data, dict) else {}
    db_url = recorder_conf.get("db_url", "") if isinstance(recorder_conf, dict) else ""

    config = {
        "rules_file": "compression_rules.json",
        "compression_tiers": [
            {"older_than_days": 365, "level": "aggressive"},
            {"older_than_days": 30, "level": "moderate"},
            {"older_than_days": 1, "level": "conservative"},
        ],
        "_config_path": str(path),
    }

    if db_url:
        db_url_clean = str(db_url).strip()
        if db_url_clean.startswith("jdbc:"):
            db_url_clean = db_url_clean[len("jdbc:"):]

        if db_url_clean.startswith(("postgresql://", "postgres://")):
            config["db_adapter"] = "postgres"
            config["postgres"] = {"url": db_url_clean}
        elif db_url_clean.startswith("sqlite:///"):
            sqlite_file = db_url_clean[len("sqlite:///"):]
            sqlite_path = Path(sqlite_file) if sqlite_file.startswith("/") else (path.parent / sqlite_file)
            config["db_adapter"] = "sqlite"
            config["sqlite"] = {"path": str(sqlite_path)}
        else:
            raise ValueError(f"Unsupported database scheme in recorder.db_url: {db_url}")
    else:
        default_sqlite = path.parent / "home-assistant_v2.db"
        config["db_adapter"] = "sqlite"
        config["sqlite"] = {"path": str(default_sqlite)}

    logger.info("Loaded Home Assistant configuration from %s (Adapter: %s)", path, config["db_adapter"])
    return config


def load_config(config_path=None):
    if config_path:
        path = Path(config_path)
    else:
        path = resolve_default_config_path()

    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found at: {path}")

    if path.suffix.lower() in (".yaml", ".yml"):
        return parse_ha_configuration_yaml(path)

    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
        cfg["_config_path"] = str(path)

        ha_yaml = cfg.get("configuration_yaml") or cfg.get("ha_config")
        if ha_yaml:
            yaml_resolved = (path.parent / ha_yaml).resolve()
            ha_cfg = parse_ha_configuration_yaml(yaml_resolved)
            for k in ("db_adapter", "postgres", "sqlite"):
                if k in ha_cfg and k not in cfg:
                    cfg[k] = ha_cfg[k]

        return cfg


def load_rules(rules_path=None):
    path = Path(rules_path) if rules_path else DEFAULT_RULES_PATH
    if not path.is_file():
        raise FileNotFoundError(f"Rules file not found at: {path}")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base, override):
    result = dict(base)
    for key, value in override.items():
        if key in result:
            if isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = deep_merge(result[key], value)
            elif isinstance(result[key], list) and isinstance(value, list):
                result[key] = list(result[key]) + [x for x in value if x not in result[key]]
            else:
                result[key] = value
        else:
            result[key] = value
    return result


def resolve_level_config(rules_data, level_identifier):
    levels = rules_data.get("levels", {})
    key = str(level_identifier).lower()

    target_key = None
    if key in levels:
        target_key = key
    else:
        for lvl_key, lvl_val in levels.items():
            if lvl_val.get("name", "").lower() == key:
                target_key = lvl_key
                break

    if target_key is None:
        available = [f"{k} ({v.get('name', '')})" for k, v in levels.items()]
        raise ValueError(f"Unknown compression level '{level_identifier}'. Available levels: {', '.join(available)}")

    chain = []
    curr = target_key
    visited = set()
    while curr:
        if curr in visited:
            raise ValueError(f"Cyclic inheritance detected in compression rules for level '{curr}'")
        visited.add(curr)
        chain.append(curr)
        parent = levels.get(curr, {}).get("inherits")
        curr = str(parent).lower() if parent is not None else None

    merged = {}
    for lvl in reversed(chain):
        merged = deep_merge(merged, levels[lvl])

    return merged


def get_connection(config):
    db_adapter = config.get("db_adapter", "postgres").lower()

    if db_adapter == "postgres":
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "psycopg2 is required for PostgreSQL connections. Install it via 'pip install psycopg2-binary'."
            ) from exc

        pg_config = config.get("postgres", {})
        raw_url = pg_config.get("url", "").strip()
        user = pg_config.get("user")
        password = pg_config.get("password")

        if raw_url:
            cleaned_url = raw_url[len("jdbc:"):] if raw_url.startswith("jdbc:") else raw_url
            parsed = urlparse(cleaned_url)

            conn_kwargs = {
                "dbname": parsed.path.lstrip("/"),
                "host": parsed.hostname,
                "port": parsed.port or 5432,
            }
            conn_user = user if user else parsed.username
            if conn_user:
                conn_kwargs["user"] = conn_user
            conn_pass = password if password else parsed.password
            if conn_pass:
                conn_kwargs["password"] = conn_pass

            return psycopg2.connect(**conn_kwargs)

        if pg_config.get("dsn"):
            return psycopg2.connect(pg_config["dsn"])

        raise ValueError("Missing PostgreSQL connection configuration ('url' or 'dsn').")

    if db_adapter == "sqlite":
        sqlite_config = config.get("sqlite", {})
        sqlite_path = sqlite_config.get("path", "home-assistant_v2.db")
        conn = sqlite3.connect(sqlite_path, timeout=60.0)
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode = WAL;")
        cur.execute("PRAGMA cache_size = -128000;")
        cur.execute("PRAGMA mmap_size = 4294967296;")
        cur.execute("PRAGMA synchronous = NORMAL;")
        cur.execute("PRAGMA temp_store = MEMORY;")
        return conn

    raise ValueError(f"Unsupported database adapter: {db_adapter}")


def get_entity_metadata_map(cursor, db_adapter):
    try:
        cursor.execute("SELECT metadata_id, entity_id FROM states_meta")
        return dict(cursor.fetchall())
    except Exception as exc:
        logger.debug("states_meta query failed (%s); trying fallback from states table", exc)

    try:
        cursor.execute("SELECT DISTINCT metadata_id, entity_id FROM states WHERE metadata_id IS NOT NULL")
        return dict(cursor.fetchall())
    except Exception as exc:
        logger.debug("Distinct entity_id query on states failed (%s); falling back to metadata_ids only", exc)

    cursor.execute("SELECT DISTINCT metadata_id FROM states WHERE metadata_id IS NOT NULL")
    return {row[0]: f"unknown.{row[0]}" for row in cursor.fetchall()}


def get_statistics_meta_units(cursor):
    """
    Reads statistic_id -> unit_of_measurement mapping from statistics_meta if available.
    """
    try:
        cursor.execute("SELECT statistic_id, unit_of_measurement FROM statistics_meta WHERE unit_of_measurement IS NOT NULL")
        return dict(cursor.fetchall())
    except Exception:
        return {}


def load_ha_device_registry_mapping(config):
    """
    Looks for .storage/core.device_registry and core.entity_registry to map entity_id -> device model.
    Checks config path parent, configuration_yaml dir, and sqlite db dir.
    """
    search_dirs = []
    # Check current directory and script directory directly
    search_dirs.append(Path.cwd().resolve())
    search_dirs.append(Path(__file__).resolve().parent)

    cfg_path = config.get("_config_path")
    if cfg_path:
        p = Path(cfg_path).resolve()
        search_dirs.extend([p.parent, p.parent.parent])

    ha_yaml = config.get("configuration_yaml") or config.get("ha_config")
    if ha_yaml:
        yp = Path(ha_yaml).resolve()
        search_dirs.extend([yp.parent, yp.parent.parent])

    sqlite_path = config.get("sqlite", {}).get("path")
    if sqlite_path:
        sp = Path(sqlite_path).resolve()
        search_dirs.extend([sp.parent, sp.parent.parent])

    entity_to_model = {}

    for s_dir in search_dirs:
        candidates = [
            (s_dir / "core.entity_registry", s_dir / "core.device_registry", s_dir),
            (s_dir / ".storage" / "core.entity_registry", s_dir / ".storage" / "core.device_registry", s_dir / ".storage"),
        ]

        for ent_reg_file, dev_reg_file, location in candidates:
            if ent_reg_file.is_file() and dev_reg_file.is_file():
                try:
                    with open(dev_reg_file, "r", encoding="utf-8") as f:
                        dev_data = json.load(f)
                    dev_map = {}  # device_id -> set of (model, model_id)
                    for dev in dev_data.get("data", {}).get("devices", []):
                        d_id = dev.get("id")
                        models = set()
                        if dev.get("model"):
                            models.add(str(dev["model"]).strip())
                        if dev.get("model_id"):
                            models.add(str(dev["model_id"]).strip())
                        if d_id and models:
                            dev_map[d_id] = models

                    with open(ent_reg_file, "r", encoding="utf-8") as f:
                        ent_data = json.load(f)
                    for ent in ent_data.get("data", {}).get("entities", []):
                        eid = ent.get("entity_id")
                        did = ent.get("device_id")
                        if eid and did and did in dev_map:
                            entity_to_model[eid] = dev_map[did]

                    if entity_to_model:
                        logger.info("Loaded %d entity-to-device-model mappings from %s", len(entity_to_model), location)
                        return entity_to_model
                except Exception as exc:
                    logger.warning("Failed to load HA device registry from %s: %s", location, exc)

    return entity_to_model


def normalize_attribute_rules(raw_rules, ignored_list=None):
    norm = {}
    if ignored_list:
        for attr in ignored_list:
            norm[attr] = {"strategy": "ignore"}
    if raw_rules:
        for attr, rule in raw_rules.items():
            if isinstance(rule, str):
                norm[attr] = {"strategy": rule.lower()}
            elif isinstance(rule, dict):
                norm[attr] = rule
    return norm


def extract_rule_body(rule_dict):
    """
    Extracts attribute rules, optional state_tolerance, and state_strategy from a rule block, excluding 'selectors'.
    """
    if not isinstance(rule_dict, dict):
        return {}, None, None
    clean = {k: v for k, v in rule_dict.items() if k != "selectors"}
    state_tol = clean.pop("state_tolerance", None)

    state_strat = clean.pop("state_strategy", None)
    if state_strat is None and "preserve_endpoints" in clean:
        pres = clean.pop("preserve_endpoints")
        state_strat = "preserve_endpoints" if pres else "first_only"
    elif state_strat is not None:
        state_strat = "preserve_endpoints" if str(state_strat).lower().strip() in ("preserve_endpoints", "endpoints", "true") else "first_only"

    attr_rules = clean.pop("attribute_rules", clean)
    return attr_rules, state_tol, state_strat


def resolve_entity_rule(entity_id, device_class, level_config, entity_model=None, unit_of_measurement=None):
    """
    Resolves compression rules for an entity using the priority order:
      1. device_attribute_rules (matching entity_id / model / device_class / domain selector)
      2. unit_attribute_rules (matching unit selector against unit_of_measurement)
      3. default_attribute_rules (fallback for unknown / baseline attributes)
      4. Backward-compatible fallback for legacy 'devices' and 'default' keys.
    """
    merged_attr_rules = {}
    merged_state_tol = None

    merged_state_strategy = level_config.get("state_strategy")
    if merged_state_strategy is None and "preserve_endpoints" in level_config:
        merged_state_strategy = "preserve_endpoints" if level_config.get("preserve_endpoints") else "first_only"
    if not merged_state_strategy:
        lvl_name = str(level_config.get("name", "")).lower()
        if lvl_name in ("conservative", "1") or str(level_config.get("level")) == "1":
            merged_state_strategy = "preserve_endpoints"
        else:
            merged_state_strategy = "first_only"
    else:
        merged_state_strategy = "preserve_endpoints" if str(merged_state_strategy).lower().strip() in ("preserve_endpoints", "endpoints", "true") else "first_only"

    # Step 3 (Baseline): default_attribute_rules or legacy level_config["default"]
    default_block = level_config.get("default_attribute_rules")
    if default_block is None:
        default_block = level_config.get("default", {})
    def_attrs, def_tol, def_strat = extract_rule_body(default_block)
    merged_attr_rules = deep_merge(merged_attr_rules, def_attrs)
    if def_tol is not None:
        merged_state_tol = def_tol
    if def_strat is not None:
        merged_state_strategy = def_strat

    # Backward compatibility: legacy domain / domain.device_class under "devices"
    legacy_devices = level_config.get("devices", {})
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "default"
    if domain in legacy_devices:
        dom_attrs, dom_tol, dom_strat = extract_rule_body(legacy_devices[domain])
        merged_attr_rules = deep_merge(merged_attr_rules, dom_attrs)
        if dom_tol is not None:
            merged_state_tol = dom_tol
        if dom_strat is not None:
            merged_state_strategy = dom_strat
    if device_class and f"{domain}.{device_class}" in legacy_devices:
        cls_attrs, cls_tol, cls_strat = extract_rule_body(legacy_devices[f"{domain}.{device_class}"])
        merged_attr_rules = deep_merge(merged_attr_rules, cls_attrs)
        if cls_tol is not None:
            merged_state_tol = cls_tol
        if cls_strat is not None:
            merged_state_strategy = cls_strat

    # Collect and score device_attribute_rules
    dev_rules_cfg = level_config.get("device_attribute_rules", [])
    if isinstance(dev_rules_cfg, dict):
        if "selectors" in dev_rules_cfg or "attribute_rules" in dev_rules_cfg:
            dev_rules_list = [dev_rules_cfg]
        else:
            dev_rules_list = list(dev_rules_cfg.values())
    elif isinstance(dev_rules_cfg, list):
        dev_rules_list = dev_rules_cfg
    else:
        dev_rules_list = []

    matched_dev_rules = []
    clean_model = str(entity_model).strip() if entity_model else ""

    for d_block in dev_rules_list:
        if not isinstance(d_block, dict):
            continue
        selectors = d_block.get("selectors", {})
        sel_entity = selectors.get("entity") or selectors.get("entity_id") or selectors.get("enity")
        sel_model = selectors.get("model") or selectors.get("device_model")
        sel_device_class = selectors.get("device_class")
        sel_domain = selectors.get("domain")

        match_score = 0
        is_match = True

        if sel_entity:
            if sel_entity == entity_id:
                match_score += 10
            elif fnmatch.fnmatch(entity_id, sel_entity):
                match_score += 8
            else:
                is_match = False

        if is_match and sel_model:
            sel_m_low = sel_model.lower()
            if isinstance(entity_model, (set, list, tuple)):
                if any(sel_m_low == str(m).lower() or sel_m_low in str(m).lower() for m in entity_model):
                    match_score += 5
                else:
                    is_match = False
            elif clean_model:
                if sel_m_low == clean_model.lower() or sel_m_low in clean_model.lower():
                    match_score += 5
                else:
                    is_match = False
            else:
                is_match = False

        if is_match and sel_device_class:
            if device_class and str(sel_device_class).lower() == str(device_class).lower():
                match_score += 2
            else:
                is_match = False

        if is_match and sel_domain:
            if sel_domain == domain:
                match_score += 1
            else:
                is_match = False

        if is_match and match_score > 0:
            matched_dev_rules.append((match_score, d_block))

    # Apply domain-only rules (match_score == 1) before unit rules
    matched_dev_rules.sort(key=lambda x: x[0])
    for m_score, d_block in matched_dev_rules:
        if m_score == 1:
            d_attrs, d_tol, d_strat = extract_rule_body(d_block)
            merged_attr_rules = deep_merge(merged_attr_rules, d_attrs)
            if d_tol is not None:
                merged_state_tol = d_tol
            if d_strat is not None:
                merged_state_strategy = d_strat

    # Step 2: unit_attribute_rules (matches unit selector against unit_of_measurement)
    # Supports single dict, list of dicts, or dict of named rule blocks
    unit_rules_cfg = level_config.get("unit_attribute_rules", [])
    if isinstance(unit_rules_cfg, dict):
        if "selectors" in unit_rules_cfg or "attribute_rules" in unit_rules_cfg:
            unit_rules_list = [unit_rules_cfg]
        else:
            unit_rules_list = list(unit_rules_cfg.values())
    elif isinstance(unit_rules_cfg, list):
        unit_rules_list = unit_rules_cfg
    else:
        unit_rules_list = []

    clean_unit = str(unit_of_measurement).strip() if unit_of_measurement else ""
    for u_block in unit_rules_list:
        if not isinstance(u_block, dict):
            continue
        selectors = u_block.get("selectors", {})
        target_unit = selectors.get("unit") or selectors.get("unit_of_measurement")
        if target_unit and clean_unit:
            t_u = str(target_unit).strip()
            # Match directly or allow C matching °C / C
            if clean_unit == t_u or clean_unit.lstrip("°") == t_u.lstrip("°"):
                u_attrs, u_tol, u_strat = extract_rule_body(u_block)
                merged_attr_rules = deep_merge(merged_attr_rules, u_attrs)
                if u_tol is not None:
                    merged_state_tol = u_tol
                if u_strat is not None:
                    merged_state_strategy = u_strat

    # Apply device_class rules (2 <= match_score < 5) after unit rules
    for m_score, d_block in matched_dev_rules:
        if 2 <= m_score < 5:
            d_attrs, d_tol, d_strat = extract_rule_body(d_block)
            merged_attr_rules = deep_merge(merged_attr_rules, d_attrs)
            if d_tol is not None:
                merged_state_tol = d_tol
            if d_strat is not None:
                merged_state_strategy = d_strat

    # Apply model-specific rules (5 <= match_score < 8)
    for m_score, d_block in matched_dev_rules:
        if 5 <= m_score < 8:
            d_attrs, d_tol, d_strat = extract_rule_body(d_block)
            merged_attr_rules = deep_merge(merged_attr_rules, d_attrs)
            if d_tol is not None:
                merged_state_tol = d_tol
            if d_strat is not None:
                merged_state_strategy = d_strat

    # Apply entity-specific / wildcard rules (match_score >= 8)
    for m_score, d_block in matched_dev_rules:
        if m_score >= 8:
            d_attrs, d_tol, d_strat = extract_rule_body(d_block)
            merged_attr_rules = deep_merge(merged_attr_rules, d_attrs)
            if d_tol is not None:
                merged_state_tol = d_tol
            if d_strat is not None:
                merged_state_strategy = d_strat

    # Backward compatibility: exact entity_id key under legacy "devices"
    if entity_id in legacy_devices:
        leg_ent_attrs, leg_ent_tol, leg_strat = extract_rule_body(legacy_devices[entity_id])
        merged_attr_rules = deep_merge(merged_attr_rules, leg_ent_attrs)
        if leg_ent_tol is not None:
            merged_state_tol = leg_ent_tol
        if leg_strat is not None:
            merged_state_strategy = leg_strat

    attr_rules = normalize_attribute_rules(merged_attr_rules)
    handled_keys = tuple(sorted(attr_rules.keys()))
    tolerance_rules = {k: r for k, r in attr_rules.items() if r.get("strategy") == "tolerance"}

    return attr_rules, merged_state_tol, handled_keys, tolerance_rules, merged_state_strategy


def states_match(state1, state2, tolerance):
    if state1 == state2:
        return True

    if tolerance == "ignore" or str(tolerance).lower() == "ignore":
        return True

    if tolerance is not None and not isinstance(tolerance, str) and tolerance > 0 and state1 is not None and state2 is not None:
        try:
            val1 = float(state1)
            val2 = float(state2)
            return abs(val1 - val2) <= tolerance
        except (ValueError, TypeError):
            return False

    return False


def fetch_entity_attributes(cursor, db_adapter, attr_ids, global_cache):
    """
    Batch fetches attributes for missing attr_ids, storing parsed dicts into global_cache.
    Avoids expensive individual queries during row iteration.
    """
    missing = [aid for aid in set(attr_ids) if aid and aid not in global_cache]
    if not missing:
        return global_cache

    chunk_size = 900 if db_adapter == "sqlite" else 5000
    for i in range(0, len(missing), chunk_size):
        chunk = missing[i:i + chunk_size]
        if db_adapter == "postgres":
            cursor.execute("SELECT attributes_id, shared_attrs FROM state_attributes WHERE attributes_id = ANY(%s)", (chunk,))
        else:
            placeholders = ",".join(["?"] * len(chunk))
            cursor.execute(f"SELECT attributes_id, shared_attrs FROM state_attributes WHERE attributes_id IN ({placeholders})", chunk)
        for aid, raw_json in cursor.fetchall():
            if isinstance(raw_json, dict):
                global_cache[aid] = raw_json
            elif raw_json:
                try:
                    global_cache[aid] = json.loads(raw_json)
                except (ValueError, TypeError):
                    global_cache[aid] = {}
            else:
                global_cache[aid] = {}

    return global_cache


def build_attribute_helpers(cursor, db_adapter):
    global_attrs_cache = {}

    def get_shared_attrs_dict(attr_id):
        if not attr_id:
            return {}
        if attr_id in global_attrs_cache:
            return global_attrs_cache[attr_id]

        query = (
            "SELECT shared_attrs FROM state_attributes WHERE attributes_id = %s"
            if db_adapter == "postgres"
            else "SELECT shared_attrs FROM state_attributes WHERE attributes_id = ?"
        )
        cursor.execute(query, (attr_id,))
        row = cursor.fetchone()

        if row and row[0]:
            try:
                res = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            except (ValueError, TypeError):
                res = {}
        else:
            res = {}
        global_attrs_cache[attr_id] = res
        return res

    @lru_cache(maxsize=400000)
    def get_filtered_attrs_tuple(attr_id, ignored_keys_tuple):
        attrs = get_shared_attrs_dict(attr_id)
        if not attrs:
            return ()
        return tuple(sorted((k, v) for k, v in attrs.items() if k not in ignored_keys_tuple))

    @lru_cache(maxsize=100000)
    def get_attribute_diff_keys(attr_id_1, attr_id_2):
        if attr_id_1 == attr_id_2:
            return frozenset()
        d1 = get_shared_attrs_dict(attr_id_1)
        d2 = get_shared_attrs_dict(attr_id_2)
        all_keys = set(d1.keys()) | set(d2.keys())
        diff = {k for k in all_keys if d1.get(k) != d2.get(k)}
        return frozenset(diff)

    def attrs_match(attr_id_1, attr_id_2, handled_keys, tolerance_rules):
        if attr_id_1 == attr_id_2:
            return True

        if get_filtered_attrs_tuple(attr_id_1, handled_keys) != get_filtered_attrs_tuple(attr_id_2, handled_keys):
            return False

        if tolerance_rules:
            d1 = get_shared_attrs_dict(attr_id_1)
            d2 = get_shared_attrs_dict(attr_id_2)
            for k, rule in tolerance_rules.items():
                val1 = d1.get(k)
                val2 = d2.get(k)
                if val1 == val2:
                    continue
                tol = rule.get("tolerance", 0.0)
                try:
                    if abs(float(val1) - float(val2)) > tol:
                        return False
                except (ValueError, TypeError):
                    if val1 != val2:
                        return False

        return True

    return global_attrs_cache, get_shared_attrs_dict, get_filtered_attrs_tuple, get_attribute_diff_keys, attrs_match


def compute_tier_cutoffs(tiers_config, now=None):
    if now is None:
        now = datetime.now()

    today_midnight = datetime.combine(now.date(), time.min)
    today_midnight_ts = today_midnight.timestamp()

    sorted_tiers = sorted(tiers_config, key=lambda t: t.get("older_than_days", 0), reverse=True)
    cutoff_list = []
    for tier in sorted_tiers:
        days = tier.get("older_than_days", 0)
        lvl = tier.get("level")
        tier_midnight = today_midnight - timedelta(days=days)
        cutoff_list.append({
            "days": days,
            "level": lvl,
            "cutoff_dt": tier_midnight,
            "cutoff_ts": tier_midnight.timestamp(),
        })

    return today_midnight_ts, cutoff_list


def get_level_for_timestamp(ts, today_midnight_ts, tier_cutoffs):
    if ts >= today_midnight_ts:
        return None

    for tier in tier_cutoffs:
        if ts < tier["cutoff_ts"]:
            return tier["level"]

    return tier_cutoffs[-1]["level"] if tier_cutoffs else None


def purge_orphaned_attributes(cursor, db_adapter, candidate_ids=None, chunk_size=2000, conn=None):
    """
    Purges unreferenced rows from state_attributes.
    When candidate_ids is provided, performs a fast indexed check against states
    and deletes unreferenced attributes within that candidate set.
    When candidate_ids is None, performs a full sweep of all unreferenced rows in state_attributes.
    Returns the total number of deleted attribute rows.
    """
    total_deleted = 0

    if candidate_ids is not None:
        candidate_list = [aid for aid in set(candidate_ids) if aid is not None]
        if not candidate_list:
            return 0

        c_chunk_size = 900 if db_adapter == "sqlite" else min(chunk_size, 5000)
        for i in range(0, len(candidate_list), c_chunk_size):
            chunk = candidate_list[i:i + c_chunk_size]
            if db_adapter == "postgres":
                cursor.execute("SELECT DISTINCT attributes_id FROM states WHERE attributes_id = ANY(%s)", (chunk,))
            else:
                placeholders = ",".join(["?"] * len(chunk))
                cursor.execute(f"SELECT DISTINCT attributes_id FROM states WHERE attributes_id IN ({placeholders})", chunk)

            still_referenced = {row[0] for row in cursor.fetchall()}
            orphans = [aid for aid in chunk if aid not in still_referenced]

            if orphans:
                if db_adapter == "postgres":
                    cursor.execute("DELETE FROM state_attributes WHERE attributes_id = ANY(%s)", (orphans,))
                else:
                    del_placeholders = ",".join(["?"] * len(orphans))
                    cursor.execute(f"DELETE FROM state_attributes WHERE attributes_id IN ({del_placeholders})", orphans)
                total_deleted += len(orphans)

        return total_deleted

    # Full sweep mode: delete in efficient batches of 50,000
    sweep_batch_size = max(chunk_size, 50000)
    sweep_query = (
        """
        DELETE FROM state_attributes
        WHERE attributes_id IN (
            SELECT a.attributes_id
            FROM state_attributes a
            LEFT JOIN states s ON a.attributes_id = s.attributes_id
            WHERE s.attributes_id IS NULL
            LIMIT %s
        )
        """
        if db_adapter == "postgres"
        else """
        DELETE FROM state_attributes
        WHERE attributes_id IN (
            SELECT a.attributes_id
            FROM state_attributes a
            LEFT JOIN states s ON a.attributes_id = s.attributes_id
            WHERE s.attributes_id IS NULL
            LIMIT ?
        )
        """
    )
    t_start = time_mod.time()
    batch_idx = 0
    while True:
        cursor.execute(sweep_query, (sweep_batch_size,))
        deleted_count = cursor.rowcount
        if deleted_count is None or deleted_count <= 0:
            break
        total_deleted += deleted_count
        batch_idx += 1
        if conn:
            conn.commit()
        elapsed = max(0.1, time_mod.time() - t_start)
        logger.info(
            "Full sweep batch #%d: deleted %d rows (%d total purged so far, %.0f rows/s)...",
            batch_idx,
            deleted_count,
            total_deleted,
            total_deleted / elapsed,
        )

    return total_deleted


def run_purge_attributes(config=None):
    if config is None:
        config = load_config()

    db_adapter = config.get("db_adapter", "postgres").lower()
    chunk_size = config.get("chunk_size", 2000)
    logger.info("Starting standalone purge of unreferenced state_attributes. DB Adapter: %s", db_adapter)

    conn = get_connection(config)
    try:
        cursor = conn.cursor()
        t0 = time_mod.time()
        deleted = purge_orphaned_attributes(cursor, db_adapter, candidate_ids=None, chunk_size=chunk_size, conn=conn)
        conn.commit()
        elapsed = time_mod.time() - t0
        logger.info("Full purge complete. Removed %d orphaned state_attributes in %.2fs.", deleted, elapsed)
        return deleted
    finally:
        conn.close()


def run_compression(config=None, rules=None, target_entity=None, limit_entities=None, purge_all_attributes=False):
    if config is None:
        config = load_config()

    if rules is None:
        rules_path = config.get("rules_file")
        if rules_path:
            base_dir = Path(config.get("_config_path", __file__)).resolve().parent
            rules_path = (base_dir / rules_path).resolve()
        rules = load_rules(rules_path)

    db_adapter = config.get("db_adapter", "postgres").lower()
    chunk_size = config.get("chunk_size", 2000)

    tiers_config = config.get("compression_tiers")
    if tiers_config:
        today_midnight_ts, tier_cutoffs = compute_tier_cutoffs(tiers_config)
        logger.info("Starting compression with TIME-BASED TIERS (aligned to midnight):")
        for t in tier_cutoffs:
            logger.info("  - Older than %3d days (before %s): Level %s", t["days"], t["cutoff_dt"].strftime("%Y-%m-%d %H:%M"), t["level"])
        logger.info("  - Events from today (since %s): Uncompressed buffer", datetime.fromtimestamp(today_midnight_ts).strftime("%Y-%m-%d 00:00"))
    else:
        level_identifier = config.get("compression_level", 2)
        level_config = resolve_level_config(rules, level_identifier)
        logger.info("Starting compression with STATIC LEVEL: %s (%s)", level_identifier, level_config.get("name", ""))
        tier_cutoffs = None
        today_midnight_ts = None

    conn = get_connection(config)
    try:
        cursor = conn.cursor()

        if db_adapter == "sqlite":
            cursor.execute("PRAGMA foreign_keys = OFF;")

        cursor.execute("SELECT COUNT(*) FROM states")
        total_db_states = cursor.fetchone()[0]

        # Unpack all helpers to allow cache clearing
        global_attrs_cache, get_shared_attrs_dict, get_filtered_attrs_tuple, get_attribute_diff_keys, attrs_match = build_attribute_helpers(cursor, db_adapter)

        metadata_entity_map = get_entity_metadata_map(cursor, db_adapter)
        total_entities_all = len(metadata_entity_map)

        if target_entity:
            entity_id_to_mid = {v: k for k, v in metadata_entity_map.items()}
            if target_entity not in entity_id_to_mid:
                raise ValueError(f"Entity '{target_entity}' not found in database.")
            mid_target = entity_id_to_mid[target_entity]
            entities_to_process = [(mid_target, target_entity)]
            logger.info("Targeting single entity: %s (metadata_id: %d, total DB states: %d)", target_entity, mid_target, total_db_states)
        elif limit_entities and limit_entities > 0:
            limit_query = "SELECT metadata_id FROM states GROUP BY metadata_id ORDER BY COUNT(*) DESC LIMIT %s" if db_adapter == "postgres" else "SELECT metadata_id FROM states GROUP BY metadata_id ORDER BY COUNT(*) DESC LIMIT ?"
            cursor.execute(limit_query, (limit_entities,))
            sampled_mids = [r[0] for r in cursor.fetchall()]
            entities_to_process = [(mid, metadata_entity_map.get(mid, f"unknown.{mid}")) for mid in sampled_mids]
            logger.info("Sample mode: processing top %d entities (out of %d total entities, %d total states in DB).", len(entities_to_process), total_entities_all, total_db_states)
        else:
            entities_to_process = list(metadata_entity_map.items())
            logger.info("Processing %d distinct entities in database (%d total states in DB).", len(entities_to_process), total_db_states)

        update_old_state_query = (
            "UPDATE states SET old_state_id = %s WHERE state_id = %s"
            if db_adapter == "postgres"
            else "UPDATE states SET old_state_id = ? WHERE state_id = ?"
        )
        update_attr_query = (
            "UPDATE states SET attributes_id = %s WHERE state_id = %s"
            if db_adapter == "postgres"
            else "UPDATE states SET attributes_id = ? WHERE state_id = ?"
        )

        total_checked = 0
        total_deleted = 0
        total_bridged = 0
        total_attr_updated = 0
        candidate_attr_ids = set()
        commit_interval_entities = 25

        cached_level_configs = {}

        def get_level_cfg(lvl_id):
            if lvl_id not in cached_level_configs:
                cached_level_configs[lvl_id] = resolve_level_config(rules, lvl_id)
            return cached_level_configs[lvl_id]

        t_comp_start = time_mod.time()

        ha_model_map = load_ha_device_registry_mapping(config)
        stat_units_map = get_statistics_meta_units(cursor)

        for idx, (mid, entity_id) in enumerate(entities_to_process, start=1):
            query = """
                SELECT state_id, state, attributes_id, old_state_id, last_updated_ts
                FROM states
                WHERE metadata_id = %s
                ORDER BY last_updated_ts ASC, state_id ASC
            """ if db_adapter == "postgres" else """
                SELECT state_id, state, attributes_id, old_state_id, last_updated_ts
                FROM states
                WHERE metadata_id = ?
                ORDER BY last_updated_ts ASC, state_id ASC
            """
            cursor.execute(query, (mid,))
            rows = cursor.fetchall()

            if not rows:
                continue

            total_checked += len(rows)

            distinct_aids = {r[2] for r in rows if r[2]}
            fetch_entity_attributes(cursor, db_adapter, distinct_aids, global_attrs_cache)

            first_attrs = get_shared_attrs_dict(rows[0][2])
            device_class = first_attrs.get("device_class")
            entity_model = ha_model_map.get(entity_id) or first_attrs.get("model") or first_attrs.get("device_model")
            unit_of_measurement = first_attrs.get("unit_of_measurement") or stat_units_map.get(entity_id)

            entity_rules_by_level = {}

            def get_entity_level_rule(lvl_id):
                if lvl_id not in entity_rules_by_level:
                    lvl_cfg = get_level_cfg(lvl_id)
                    entity_rules_by_level[lvl_id] = resolve_entity_rule(
                        entity_id,
                        device_class,
                        lvl_cfg,
                        entity_model=entity_model,
                        unit_of_measurement=unit_of_measurement,
                    )
                return entity_rules_by_level[lvl_id]

            entity_states_to_delete = []
            entity_old_state_updates = []
            entity_attr_updates = []

            last_kept_id, last_kept_state, last_kept_attr, _, last_kept_ts = rows[0]
            dup_buffer = []
            curr_state_strategy = "first_only"
            has_smart_update = False

            def flush_dup_buffer(is_entity_end=False):
                nonlocal last_kept_id, last_kept_state, last_kept_attr
                if not dup_buffer:
                    return
                # At the end of an entity, the active head (last state) must never be deleted
                if is_entity_end or curr_state_strategy == "preserve_endpoints":
                    if len(dup_buffer) >= 2:
                        entity_states_to_delete.extend([d[0] for d in dup_buffer[:-1]])
                        candidate_attr_ids.update([d[2] for d in dup_buffer[:-1] if d[2]])
                        endpoint = dup_buffer[-1]
                        if endpoint[3] != last_kept_id:
                            entity_old_state_updates.append((last_kept_id, endpoint[0]))
                        last_kept_id = endpoint[0]
                        last_kept_state = endpoint[1]
                        last_kept_attr = endpoint[2]
                    elif len(dup_buffer) == 1:
                        endpoint = dup_buffer[0]
                        if endpoint[3] != last_kept_id:
                            entity_old_state_updates.append((last_kept_id, endpoint[0]))
                        last_kept_id = endpoint[0]
                        last_kept_state = endpoint[1]
                        last_kept_attr = endpoint[2]
                else:
                    entity_states_to_delete.extend([d[0] for d in dup_buffer])
                    candidate_attr_ids.update([d[2] for d in dup_buffer if d[2]])
                    if has_smart_update and dup_buffer[-1][2] != last_kept_attr:
                        entity_attr_updates.append((dup_buffer[-1][2], last_kept_id))
                        if last_kept_attr:
                            candidate_attr_ids.add(last_kept_attr)
                dup_buffer.clear()

            for i in range(1, len(rows)):
                curr_id, curr_state, curr_attr, curr_old, curr_ts = rows[i]

                if tier_cutoffs:
                    active_lvl = get_level_for_timestamp(curr_ts, today_midnight_ts, tier_cutoffs)
                else:
                    active_lvl = level_identifier

                if active_lvl is None:
                    flush_dup_buffer(is_entity_end=False)
                    if curr_old != last_kept_id:
                        entity_old_state_updates.append((last_kept_id, curr_id))

                    last_kept_id = curr_id
                    last_kept_state = curr_state
                    last_kept_attr = curr_attr
                    last_kept_ts = curr_ts
                    continue

                attr_rules, tolerance, handled_keys, tolerance_rules, state_strategy = get_entity_level_rule(active_lvl)
                has_smart_update = any(r.get("strategy") in ("last", "max") for r in attr_rules.values())
                curr_state_strategy = state_strategy

                if states_match(last_kept_state, curr_state, tolerance) and attrs_match(last_kept_attr, curr_attr, handled_keys, tolerance_rules):
                    dup_buffer.append((curr_id, curr_state, curr_attr, curr_old, curr_ts))
                else:
                    flush_dup_buffer(is_entity_end=False)
                    if curr_old != last_kept_id:
                        entity_old_state_updates.append((last_kept_id, curr_id))

                    last_kept_id = curr_id
                    last_kept_state = curr_state
                    last_kept_attr = curr_attr
                    last_kept_ts = curr_ts

            flush_dup_buffer(is_entity_end=True)

            if entity_attr_updates:
                cursor.executemany(update_attr_query, entity_attr_updates)
                total_attr_updated += len(entity_attr_updates)

            if entity_old_state_updates:
                cursor.executemany(update_old_state_query, entity_old_state_updates)
                total_bridged += len(entity_old_state_updates)

            if entity_states_to_delete:
                for chunk_start in range(0, len(entity_states_to_delete), chunk_size):
                    chunk = entity_states_to_delete[chunk_start:chunk_start + chunk_size]
                    if db_adapter == "postgres":
                        # Dissolve inbound foreign key references before deleting states in chunk
                        cursor.execute("UPDATE states SET old_state_id = NULL WHERE old_state_id = ANY(%s)", (chunk,))
                        cursor.execute("DELETE FROM states WHERE state_id = ANY(%s)", (chunk,))
                    else:
                        placeholders = ",".join(["?"] * len(chunk))
                        cursor.execute(f"DELETE FROM states WHERE state_id IN ({placeholders})", chunk)
                total_deleted += len(entity_states_to_delete)

            if idx % commit_interval_entities == 0 or idx == len(entities_to_process):
                conn.commit()
                elapsed_now = max(0.1, time_mod.time() - t_comp_start)
                rate = total_checked / elapsed_now
                logger.info(
                    "Progress: %d/%d entities (%.1f%%) | Checked: %d | Deleted: %d | Bridged: %d | Smart Attrs: %d | Speed: %.0f rows/s",
                    idx,
                    len(entities_to_process),
                    (idx / len(entities_to_process)) * 100,
                    total_checked,
                    total_deleted,
                    total_bridged,
                    total_attr_updated,
                    rate,
                )

            # Prevent memory leaks by flushing caches after processing each entity
            global_attrs_cache.clear()
            get_filtered_attrs_tuple.cache_clear()
            get_attribute_diff_keys.cache_clear()

        conn.commit()

        total_attr_deleted = 0
        if candidate_attr_ids:
            logger.info("Purging orphaned state_attributes (evaluating %d candidate attributes)...", len(candidate_attr_ids))
            total_attr_deleted += purge_orphaned_attributes(
                cursor, db_adapter, candidate_ids=candidate_attr_ids, chunk_size=chunk_size
            )
            conn.commit()

        if purge_all_attributes or config.get("purge_attributes") or config.get("purge_orphaned_attributes"):
            logger.info("Performing full sweep for any remaining unreferenced state_attributes...")
            full_sweep_deleted = purge_orphaned_attributes(
                cursor, db_adapter, candidate_ids=None, chunk_size=chunk_size, conn=conn
            )
            conn.commit()
            total_attr_deleted += full_sweep_deleted
            logger.info("Full sweep complete. Purged %d unreferenced state_attributes.", full_sweep_deleted)

        t_comp_elapsed = time_mod.time() - t_comp_start
        reduction = (total_deleted / total_checked * 100) if total_checked > 0 else 0.0
        overall_rate = (total_checked / t_comp_elapsed) if t_comp_elapsed > 0 else 0.0

        logger.info(
            "Compression complete in %.2fs (%.0f rows/s). Checked: %d | Deleted states: %d (%.1f%% reduction) | Deleted attributes: %d | Bridged: %d | Smart Attrs Updated: %d",
            t_comp_elapsed,
            overall_rate,
            total_checked,
            total_deleted,
            reduction,
            total_attr_deleted,
            total_bridged,
            total_attr_updated,
        )

        if total_checked < total_db_states:
            est_total_time = (total_db_states / overall_rate) if overall_rate > 0 else 0
            m = int(est_total_time // 60)
            s = int(est_total_time % 60)
            logger.info(
                "Extrapolation for entire DB (%d states): estimated execution time ~%dm %02ds.",
                total_db_states,
                m,
                s,
            )
    finally:
        conn.close()


def run_simulation(config=None, rules=None, show_details=False, limit_entities=None, target_entity=None):
    if config is None:
        config = load_config()

    if rules is None:
        rules_path = config.get("rules_file")
        if rules_path:
            base_dir = Path(config.get("_config_path", __file__)).resolve().parent
            rules_path = (base_dir / rules_path).resolve()
        rules = load_rules(rules_path)

    db_adapter = config.get("db_adapter", "postgres").lower()
    logger.info("Starting COMPRESSION SIMULATION. DB Adapter: %s", db_adapter)

    lvl1_cfg = resolve_level_config(rules, 1)
    lvl2_cfg = resolve_level_config(rules, 2)
    lvl3_cfg = resolve_level_config(rules, 3)

    conn = get_connection(config)
    try:
        cursor = conn.cursor()
        global_attrs_cache, get_shared_attrs_dict, _, _, attrs_match = build_attribute_helpers(cursor, db_adapter)

        cursor.execute("SELECT last_updated_ts FROM states ORDER BY last_updated_ts ASC LIMIT 1")
        min_row = cursor.fetchone()
        min_ts = min_row[0] if min_row else None

        cursor.execute("SELECT last_updated_ts FROM states ORDER BY last_updated_ts DESC LIMIT 1")
        max_row = cursor.fetchone()
        max_ts = max_row[0] if max_row else None

        min_dt = datetime.fromtimestamp(min_ts) if min_ts else None
        max_dt = datetime.fromtimestamp(max_ts) if max_ts else None

        now = datetime.now()
        is_archive = max_dt and (now - max_dt).days > 30
        ref_now = max_dt if is_archive else now
        tiers_config = config.get("compression_tiers") or [
            {"older_than_days": 365, "level": "aggressive"},
            {"older_than_days": 30, "level": "moderate"},
            {"older_than_days": 1, "level": "conservative"},
        ]
        today_midnight_ts, tier_cutoffs = compute_tier_cutoffs(tiers_config, now=ref_now)

        metadata_entity_map = get_entity_metadata_map(cursor, db_adapter)
        total_entities_all = len(metadata_entity_map)

        if target_entity:
            entity_id_to_mid = {v: k for k, v in metadata_entity_map.items()}
            if any(ch in target_entity for ch in "*?[]"):
                mids_to_process = [k for k, v in metadata_entity_map.items() if fnmatch.fnmatch(v, target_entity)]
                if not mids_to_process:
                    raise ValueError(f"No entities matching pattern '{target_entity}' found in database.")
            elif target_entity not in entity_id_to_mid:
                raise ValueError(f"Entity '{target_entity}' not found in database.")
            else:
                mids_to_process = [entity_id_to_mid[target_entity]]
        elif limit_entities and limit_entities > 0:
            limit_query = "SELECT metadata_id FROM states GROUP BY metadata_id ORDER BY COUNT(*) DESC LIMIT %s" if db_adapter == "postgres" else "SELECT metadata_id FROM states GROUP BY metadata_id ORDER BY COUNT(*) DESC LIMIT ?"
            cursor.execute(limit_query, (limit_entities,))
            mids_to_process = [r[0] for r in cursor.fetchall()]
        else:
            mids_to_process = list(metadata_entity_map.keys())

        # Total states in whole DB for extrapolation
        cursor.execute("SELECT COUNT(*) FROM states")
        total_db_states = cursor.fetchone()[0]

        print("\n" + "=" * 105)
        print(" HOMEASSISTANT-SMARTCOMPRESS - DATABASE COMPRESSION POTENTIAL SIMULATION")
        print("=" * 105)
        db_source = config.get("sqlite", {}).get("path") or config.get("postgres", {}).get("url", "PostgreSQL")
        print(f" Database:             {db_adapter.upper()} ({db_source})")
        print(f" Total Entities:       {len(mids_to_process):,}" + (f" (Sample out of {total_entities_all:,} total entities in DB)" if (limit_entities or target_entity) else ""))
        if min_dt and max_dt:
            days_span = (max_ts - min_ts) / 86400
            print(f" Recorded Timespan:    {min_dt.strftime('%Y-%m-%d %H:%M')} to {max_dt.strftime('%Y-%m-%d %H:%M')} ({days_span:.1f} days)")
        if is_archive:
            print(f" Tier Reference Date:  {ref_now.strftime('%Y-%m-%d 00:00')} (auto-anchored to latest recorded date)")
        print("-" * 105)

        deleted_baseline = 0
        deleted_lvl1 = 0
        deleted_lvl2 = 0
        deleted_lvl3 = 0
        deleted_tiered = 0
        total_checked = 0

        entity_details_list = []
        t_start = time_mod.time()
        last_log_time = t_start

        # Measure per-strategy CPU evaluation times
        t_eval_base = 0.0
        t_eval_l1 = 0.0
        t_eval_l2 = 0.0
        t_eval_l3 = 0.0
        t_eval_tier = 0.0

        ha_model_map = load_ha_device_registry_mapping(config)
        stat_units_map = get_statistics_meta_units(cursor)

        for idx, mid in enumerate(mids_to_process, start=1):
            entity_id = metadata_entity_map.get(mid, f"unknown.{mid}")
            query = """
                SELECT state_id, state, attributes_id, old_state_id, last_updated_ts
                FROM states
                WHERE metadata_id = %s
                ORDER BY last_updated_ts ASC, state_id ASC
            """ if db_adapter == "postgres" else """
                SELECT state_id, state, attributes_id, old_state_id, last_updated_ts
                FROM states
                WHERE metadata_id = ?
                ORDER BY last_updated_ts ASC, state_id ASC
            """
            cursor.execute(query, (mid,))
            rows = cursor.fetchall()

            if not rows or len(rows) <= 1:
                if rows:
                    total_checked += len(rows)
                continue

            ent_count = len(rows)
            total_checked += ent_count

            distinct_aids = {r[2] for r in rows if r[2]}
            fetch_entity_attributes(cursor, db_adapter, distinct_aids, global_attrs_cache)

            first_attrs = get_shared_attrs_dict(rows[0][2])
            dev_class = first_attrs.get("device_class")
            entity_model = ha_model_map.get(entity_id) or first_attrs.get("model") or first_attrs.get("device_model")
            unit_of_measurement = first_attrs.get("unit_of_measurement") or stat_units_map.get(entity_id)

            r1, t1, hk1, tr1, s1 = resolve_entity_rule(entity_id, dev_class, lvl1_cfg, entity_model=entity_model, unit_of_measurement=unit_of_measurement)
            r2, t2, hk2, tr2, s2 = resolve_entity_rule(entity_id, dev_class, lvl2_cfg, entity_model=entity_model, unit_of_measurement=unit_of_measurement)
            r3, t3, hk3, tr3, s3 = resolve_entity_rule(entity_id, dev_class, lvl3_cfg, entity_model=entity_model, unit_of_measurement=unit_of_measurement)

            tier_rules = {
                1: (t1, hk1, tr1, s1), "1": (t1, hk1, tr1, s1), "conservative": (t1, hk1, tr1, s1),
                2: (t2, hk2, tr2, s2), "2": (t2, hk2, tr2, s2), "moderate": (t2, hk2, tr2, s2),
                3: (t3, hk3, tr3, s3), "3": (t3, hk3, tr3, s3), "aggressive": (t3, hk3, tr3, s3),
            }

            ent_base = 0
            ent_l1 = 0
            ent_l2 = 0
            ent_l3 = 0
            ent_tier = 0

            # Baseline
            t0 = time_mod.perf_counter()
            base_state, base_attr = rows[0][1], rows[0][2]
            for r in rows[1:]:
                if r[1] == base_state and r[2] == base_attr:
                    ent_base += 1
                else:
                    base_state, base_attr = r[1], r[2]
            t_eval_base += (time_mod.perf_counter() - t0)

            # Level 1
            t0 = time_mod.perf_counter()
            k_s1, k_a1 = rows[0][1], rows[0][2]
            dup_l1 = 0
            for r in rows[1:]:
                if states_match(k_s1, r[1], t1) and attrs_match(k_a1, r[2], hk1, tr1):
                    dup_l1 += 1
                else:
                    if s1 == "preserve_endpoints":
                        if dup_l1 >= 2:
                            ent_l1 += (dup_l1 - 1)
                    else:
                        ent_l1 += dup_l1
                    dup_l1 = 0
                    k_s1, k_a1 = r[1], r[2]
            if dup_l1 >= 2:
                ent_l1 += (dup_l1 - 1)
            t_eval_l1 += (time_mod.perf_counter() - t0)

            # Level 2
            t0 = time_mod.perf_counter()
            k_s2, k_a2 = rows[0][1], rows[0][2]
            dup_l2 = 0
            for r in rows[1:]:
                if states_match(k_s2, r[1], t2) and attrs_match(k_a2, r[2], hk2, tr2):
                    dup_l2 += 1
                else:
                    if s2 == "preserve_endpoints":
                        if dup_l2 >= 2:
                            ent_l2 += (dup_l2 - 1)
                    else:
                        ent_l2 += dup_l2
                    dup_l2 = 0
                    k_s2, k_a2 = r[1], r[2]
            if dup_l2 >= 2:
                ent_l2 += (dup_l2 - 1)
            t_eval_l2 += (time_mod.perf_counter() - t0)

            # Level 3
            t0 = time_mod.perf_counter()
            k_s3, k_a3 = rows[0][1], rows[0][2]
            dup_l3 = 0
            for r in rows[1:]:
                if states_match(k_s3, r[1], t3) and attrs_match(k_a3, r[2], hk3, tr3):
                    dup_l3 += 1
                else:
                    if s3 == "preserve_endpoints":
                        if dup_l3 >= 2:
                            ent_l3 += (dup_l3 - 1)
                    else:
                        ent_l3 += dup_l3
                    dup_l3 = 0
                    k_s3, k_a3 = r[1], r[2]
            if dup_l3 >= 2:
                ent_l3 += (dup_l3 - 1)
            t_eval_l3 += (time_mod.perf_counter() - t0)

            # Tiered
            t0 = time_mod.perf_counter()
            k_st, k_at = rows[0][1], rows[0][2]
            dup_lt = 0
            curr_tier_strat = "first_only"
            for r in rows[1:]:
                t_lvl = get_level_for_timestamp(r[4], today_midnight_ts, tier_cutoffs)
                if t_lvl is None:
                    if curr_tier_strat == "preserve_endpoints":
                        if dup_lt >= 2:
                            ent_tier += (dup_lt - 1)
                    else:
                        ent_tier += dup_lt
                    dup_lt = 0
                    k_st, k_at = r[1], r[2]
                    continue
                tt, hkt, trt, st = tier_rules[t_lvl]
                curr_tier_strat = st
                if states_match(k_st, r[1], tt) and attrs_match(k_at, r[2], hkt, trt):
                    dup_lt += 1
                else:
                    if curr_tier_strat == "preserve_endpoints":
                        if dup_lt >= 2:
                            ent_tier += (dup_lt - 1)
                    else:
                        ent_tier += dup_lt
                    dup_lt = 0
                    k_st, k_at = r[1], r[2]
            if dup_lt >= 2:
                ent_tier += (dup_lt - 1)
            t_eval_tier += (time_mod.perf_counter() - t0)

            deleted_baseline += ent_base
            deleted_lvl1 += ent_l1
            deleted_lvl2 += ent_l2
            deleted_lvl3 += ent_l3
            deleted_tiered += ent_tier

            first_ts = rows[0][4]
            last_ts = rows[-1][4]
            ent_span_days = max(1.0, (last_ts - first_ts) / 86400.0) if (last_ts and first_ts) else 1.0

            entity_details_list.append({
                "entity_id": entity_id,
                "metadata_id": mid,
                "total": ent_count,
                "days": ent_span_days,
                "baseline": ent_base,
                "l1": ent_l1,
                "l2": ent_l2,
                "l3": ent_l3,
                "tiered": ent_tier,
            })

            if time_mod.time() - last_log_time >= 5.0 or idx == len(mids_to_process):
                last_log_time = time_mod.time()
                pct = (idx / len(mids_to_process)) * 100
                logger.info("Simulating: %d/%d entities (%.1f%%) | Checked: %d rows", idx, len(mids_to_process), pct, total_checked)

        elapsed_total = time_mod.time() - t_start

        if show_details and entity_details_list:
            filtered_details = [x for x in entity_details_list if (x["total"] / x["days"]) > 1.0]
            sorted_details = sorted(filtered_details, key=lambda x: x["total"], reverse=True)
            print("\n" + "=" * 120)
            print(f" DETAILED ENTITY LEVEL BREAKDOWN (> 1.0 entries/day based on entity lifespan: {len(sorted_details)} of {len(entity_details_list)} entities)")
            print("=" * 120)
            print(f" {'ENTITY ID':<44} | {'ROWS':>8} | {'SPAN':>6} | {'AVG/DAY':>9} | {'LEVEL 1':>9} | {'LEVEL 2':>9} | {'LEVEL 3':>9} | {'TIERED':>9}")
            print("-" * 120)
            for item in sorted_details:
                tot = item["total"]
                span_str = f"{item['days']:4.1f}d"
                avg_day_str = f"{tot / item['days']:7.1f}"
                p_1 = f"{item['l1']/tot*100:4.1f}%"
                p_2 = f"{item['l2']/tot*100:4.1f}%"
                p_3 = f"{item['l3']/tot*100:4.1f}%"
                p_t = f"{item['tiered']/tot*100:4.1f}%"
                eid = (item['entity_id'][:41] + '..') if len(item['entity_id']) > 43 else item['entity_id']
                print(f" {eid:<44} | {tot:>8,} | {span_str:>6} | {avg_day_str:>9} | {p_1:>9} | {p_2:>9} | {p_3:>9} | {p_t:>9}")
            print("=" * 120)

        # Baseline IO read speed: data retrieval throughput
        data_read_time = max(0.01, elapsed_total - (t_eval_base + t_eval_l1 + t_eval_l2 + t_eval_l3 + t_eval_tier))
        rows_per_sec_sim = (total_checked / elapsed_total) if elapsed_total > 0 else 0

        # Estimate full execution write runtimes
        # Write operations (DELETE chunked + state pointer updates) typically run at ~3,500 - 8,000 deleted rows/sec on SQLite/PostgreSQL
        # Here we calibrate based on observed batch write throughput (~5,000 deletions/sec) plus scan time:
        def estimate_full_compress_duration(deleted_count, scan_states):
            scan_time = (scan_states / (total_checked / data_read_time)) if total_checked > 0 else 0
            # Extrapolate deletion count for full database if run was on a sample
            sample_ratio = (scan_states / total_checked) if total_checked > 0 else 1.0
            extrapolated_deletes = deleted_count * sample_ratio
            write_time = extrapolated_deletes / 5000.0  # conservative write benchmark
            return scan_time + write_time

        def fmt_duration(secs):
            if secs < 60:
                return f"{secs:4.1f}s"
            m = int(secs // 60)
            s = int(secs % 60)
            if m < 60:
                return f"{m}m {s:02d}s"
            h = int(m // 60)
            m = m % 60
            return f"{h}h {m:02d}m"

        def fmt_row(name, deleted, est_secs, desc):
            rem = total_checked - deleted
            pct = (deleted / total_checked * 100) if total_checked else 0.0
            est_str = fmt_duration(est_secs)
            return f" {name:<23} | {deleted:>12,} | {rem:>14,} | {pct:>10.2f}% | {est_str:>11} | {desc}"

        sample_notice = f" (Sample: {total_checked:,} of {total_db_states:,} states in DB)" if total_checked < total_db_states else ""
        print("\n" + "=" * 122)
        print(f" SIMULATION SUMMARY RESULTS & RUNTIME ESTIMATES{sample_notice}")
        print("=" * 122)
        print(" SETTING                 | DELETED ROWS | REMAINING ROWS | SAVINGS (%) | EST. RUNTIME| DESCRIPTION")
        print("-" * 122)
        est_base = estimate_full_compress_duration(deleted_baseline, total_db_states)
        est_l1 = estimate_full_compress_duration(deleted_lvl1, total_db_states)
        est_l2 = estimate_full_compress_duration(deleted_lvl2, total_db_states)
        est_l3 = estimate_full_compress_duration(deleted_lvl3, total_db_states)
        est_tier = estimate_full_compress_duration(deleted_tiered, total_db_states)

        print(fmt_row("Baseline (Exact Match)", deleted_baseline, est_base, "Identical states & exact identical attributes"))
        print(fmt_row("Level 1 (Conservative)", deleted_lvl1, est_l1, "Telemetry noise, uptime/last_seen & battery jitter"))
        print(fmt_row("Level 2 (Moderate)", deleted_lvl2, est_l2, "Inherits L1 + analog deadbands (temp, power, volt)"))
        print(fmt_row("Level 3 (Aggressive)", deleted_lvl3, est_l3, "Inherits L2 + wider sensor deadbands & network attrs"))
        print(fmt_row("Configured Tiers", deleted_tiered, est_tier, "Age-based (>=365d: L3, >=30d: L2, >=1d: L1, <1d: raw)"))
        print("=" * 122)
        print(f" Simulation throughput: {total_checked:,} rows in {elapsed_total:.2f}s ({rows_per_sec_sim:,.0f} rows/s).")
        if total_checked < total_db_states:
            pct_sample = (total_checked / total_db_states) * 100
            print(f" 'EST. RUNTIME' extrapolates the complete write-compression run for all {total_db_states:,} states ({pct_sample:.1f}% sample size analyzed).\n")
        else:
            print(f" 'EST. RUNTIME' estimates the actual execution run (read scan + batched delete/updates) for all {total_db_states:,} states.\n")

        return {
            "total_states": total_checked,
            "baseline": {"deleted": deleted_baseline, "pct": (deleted_baseline / total_checked * 100) if total_checked else 0},
            "level_1": {"deleted": deleted_lvl1, "pct": (deleted_lvl1 / total_checked * 100) if total_checked else 0},
            "level_2": {"deleted": deleted_lvl2, "pct": (deleted_lvl2 / total_checked * 100) if total_checked else 0},
            "level_3": {"deleted": deleted_lvl3, "pct": (deleted_lvl3 / total_checked * 100) if total_checked else 0},
            "tiered": {"deleted": deleted_tiered, "pct": (deleted_tiered / total_checked * 100) if total_checked else 0},
        }
    finally:
        conn.close()


def run_analysis(config=None, rules=None, top_entities=None, top_attributes=None, target_entity=None):
    if config is None:
        config = load_config()

    if rules is None:
        rules_path = config.get("rules_file")
        if rules_path:
            base_dir = Path(config.get("_config_path", __file__)).resolve().parent
            rules_path = (base_dir / rules_path).resolve()
        rules = load_rules(rules_path)

    analysis_cfg = config.get("analysis", {})
    if top_entities is None:
        top_entities = analysis_cfg.get("top_entities", 20)
    if top_attributes is None:
        top_attributes = analysis_cfg.get("top_attributes", 5)

    db_adapter = config.get("db_adapter", "postgres").lower()
    tiers_config = config.get("compression_tiers")

    if tiers_config:
        today_midnight_ts, tier_cutoffs = compute_tier_cutoffs(tiers_config)
        tier_mode_str = f"Tiers (>=365d: {tier_cutoffs[0]['level']}, >=30d: {tier_cutoffs[1]['level']}, >=1d: {tier_cutoffs[2]['level']})"
    else:
        level_identifier = config.get("compression_level", 2)
        tier_mode_str = f"Level {level_identifier}"
        tier_cutoffs = None
        today_midnight_ts = None

    logger.info(
        "Starting ANALYSIS MODE (read-only). DB Adapter: %s | %s | Top Entities: %s | Top Attributes: %d",
        db_adapter,
        tier_mode_str,
        str(top_entities) if top_entities else "all",
        top_attributes,
    )

    conn = get_connection(config)
    try:
        cursor = conn.cursor()
        global_attrs_cache, get_shared_attrs_dict, _, get_attribute_diff_keys, attrs_match = build_attribute_helpers(cursor, db_adapter)

        metadata_entity_map = get_entity_metadata_map(cursor, db_adapter)

        if target_entity:
            entity_id_to_mid = {v: k for k, v in metadata_entity_map.items()}
            if target_entity not in entity_id_to_mid:
                raise ValueError(f"Entity '{target_entity}' not found in database.")
            mid_target = entity_id_to_mid[target_entity]
            cnt_query = "SELECT COUNT(*) FROM states WHERE metadata_id = %s" if db_adapter == "postgres" else "SELECT COUNT(*) FROM states WHERE metadata_id = ?"
            cursor.execute(cnt_query, (mid_target,))
            cnt = cursor.fetchone()[0]
            ranked_entities = [(mid_target, cnt)]
        else:
            ranking_query = """
                SELECT metadata_id, COUNT(*) AS state_count
                FROM states
                WHERE metadata_id IS NOT NULL
                GROUP BY metadata_id
                ORDER BY state_count DESC
            """
            if top_entities and top_entities > 0:
                ranking_query += f" LIMIT {int(top_entities)}"
            cursor.execute(ranking_query)
            ranked_entities = cursor.fetchall()

        if not ranked_entities:
            logger.warning("No state records found to analyze.")
            return {}

        total_analyzed_states = 0
        total_baseline_deletable = 0
        global_attr_counter = Counter()
        entity_summaries = []

        cached_level_configs = {}

        def get_level_cfg(lvl_id):
            if lvl_id not in cached_level_configs:
                cached_level_configs[lvl_id] = resolve_level_config(rules, lvl_id)
            return cached_level_configs[lvl_id]

        def simulate_reduction(rows, test_rules, tolerance, handled_keys, tolerance_rules):
            deleted_count = 0
            last_kept_state = rows[0][1]
            last_kept_attr = rows[0][2]

            for k in range(1, len(rows)):
                curr_state = rows[k][1]
                curr_attr = rows[k][2]
                curr_ts = rows[k][4]

                if tier_cutoffs and get_level_for_timestamp(curr_ts, today_midnight_ts, tier_cutoffs) is None:
                    last_kept_state = curr_state
                    last_kept_attr = curr_attr
                    continue

                if states_match(last_kept_state, curr_state, tolerance) and attrs_match(last_kept_attr, curr_attr, handled_keys, tolerance_rules):
                    deleted_count += 1
                else:
                    last_kept_state = curr_state
                    last_kept_attr = curr_attr

            return deleted_count

        ha_model_map = load_ha_device_registry_mapping(config)
        stat_units_map = get_statistics_meta_units(cursor)

        print("\n" + "=" * 90)
        print(" HOMEASSISTANT-SMARTCOMPRESS - DATABASE COMPRESSION ANALYSIS REPORT")
        print("=" * 90)

        for rank, (mid, state_count) in enumerate(ranked_entities, start=1):
            entity_id = metadata_entity_map.get(mid, f"unknown.{mid}")
            total_analyzed_states += state_count

            query = """
                SELECT state_id, state, attributes_id, old_state_id, last_updated_ts
                FROM states
                WHERE metadata_id = %s
                ORDER BY last_updated_ts ASC, state_id ASC
            """ if db_adapter == "postgres" else """
                SELECT state_id, state, attributes_id, old_state_id, last_updated_ts
                FROM states
                WHERE metadata_id = ?
                ORDER BY last_updated_ts ASC, state_id ASC
            """
            cursor.execute(query, (mid,))
            rows = cursor.fetchall()

            if not rows or len(rows) <= 1:
                continue

            distinct_aids = {r[2] for r in rows if r[2]}
            fetch_entity_attributes(cursor, db_adapter, distinct_aids, global_attrs_cache)

            first_attrs = get_shared_attrs_dict(rows[0][2])
            device_class = first_attrs.get("device_class")
            entity_model = ha_model_map.get(entity_id) or first_attrs.get("model") or first_attrs.get("device_model")
            unit_of_measurement = first_attrs.get("unit_of_measurement") or stat_units_map.get(entity_id)

            ref_lvl = tier_cutoffs[0]["level"] if tier_cutoffs else config.get("compression_level", 2)
            _, tolerance, hk, tr, _ = resolve_entity_rule(
                entity_id,
                device_class,
                get_level_cfg(ref_lvl),
                entity_model=entity_model,
                unit_of_measurement=unit_of_measurement,
            )

            entity_attr_counter = Counter()
            entity_attr_deltas = {}  # attr -> list of numeric abs deltas
            entity_attr_num_values = {}  # attr -> set of numeric values seen
            entity_attr_samples = {}  # attr -> set of sample values (up to 5)
            identical_state_transitions = 0

            for i in range(1, len(rows)):
                curr_ts = rows[i][4]
                if tier_cutoffs and get_level_for_timestamp(curr_ts, today_midnight_ts, tier_cutoffs) is None:
                    continue

                prev_state = rows[i - 1][1]
                curr_state = rows[i][1]

                if states_match(prev_state, curr_state, tolerance):
                    identical_state_transitions += 1
                    prev_attr_id = rows[i - 1][2]
                    curr_attr_id = rows[i][2]

                    if prev_attr_id != curr_attr_id:
                        d_prev = get_shared_attrs_dict(prev_attr_id)
                        d_curr = get_shared_attrs_dict(curr_attr_id)
                        diff = get_attribute_diff_keys(prev_attr_id, curr_attr_id)
                        for attr_key in diff:
                            entity_attr_counter[attr_key] += 1
                            global_attr_counter[attr_key] += 1

                            v_prev = d_prev.get(attr_key)
                            v_curr = d_curr.get(attr_key)
                            try:
                                n_p = float(v_prev)
                                n_c = float(v_curr)
                                delta = abs(n_c - n_p)
                                if attr_key not in entity_attr_deltas:
                                    entity_attr_deltas[attr_key] = []
                                    entity_attr_num_values[attr_key] = set()
                                if len(entity_attr_deltas[attr_key]) < 2000:
                                    entity_attr_deltas[attr_key].append(delta)
                                if len(entity_attr_num_values[attr_key]) < 500:
                                    entity_attr_num_values[attr_key].add(n_c)
                                    entity_attr_num_values[attr_key].add(n_p)
                            except (ValueError, TypeError):
                                pass

                            if attr_key not in entity_attr_samples:
                                entity_attr_samples[attr_key] = set()
                            if len(entity_attr_samples[attr_key]) < 4 and v_curr is not None:
                                entity_attr_samples[attr_key].add(str(v_curr)[:25])

            baseline_deleted = simulate_reduction(rows, {}, tolerance, (), {})
            total_baseline_deletable += baseline_deleted

            top_attrs = [k for k, _ in entity_attr_counter.most_common(top_attributes)]

            single_attr_reductions = {}
            for attr in top_attrs:
                single_attr_reductions[attr] = simulate_reduction(rows, {attr: {"strategy": "ignore"}}, tolerance, (attr,), {})

            cumulative_reductions = []
            cum_rules = {}
            for attr in top_attrs:
                cum_rules[attr] = {"strategy": "ignore"}
                count_saved = simulate_reduction(rows, cum_rules, tolerance, tuple(sorted(cum_rules.keys())), {})
                cumulative_reductions.append((attr, count_saved))

            entity_info = {
                "rank": rank,
                "metadata_id": mid,
                "entity_id": entity_id,
                "device_class": device_class,
                "total_states": state_count,
                "identical_state_events": identical_state_transitions,
                "baseline_duplicates": baseline_deleted,
                "top_attributes": top_attrs,
                "single_reductions": single_attr_reductions,
                "cumulative_reductions": cumulative_reductions,
            }
            entity_summaries.append(entity_info)

            domain = entity_id.split(".", 1)[0]
            dev_str = f" | Class: {device_class}" if device_class else ""
            print(f"\n[{rank}/{len(ranked_entities)}] Entity: {entity_id} (metadata_id: {mid})")
            print(f"      Domain: {domain}{dev_str} | Total states: {state_count:,}")
            dup_state_pct = (identical_state_transitions / state_count * 100) if state_count else 0
            base_pct = (baseline_deleted / state_count * 100) if state_count else 0
            print(f"      Consecutive events with same state: {identical_state_transitions:,} ({dup_state_pct:.1f}%)")
            print(f"      Baseline duplicates (identical attributes): {baseline_deleted:,} ({base_pct:.1f}% reduction)")

            if top_attrs:
                print(f"      Top {len(top_attrs)} most frequently changing attributes causing events:")
                for attr_name in top_attrs:
                    occurrences = entity_attr_counter[attr_name]
                    saved = single_attr_reductions[attr_name]
                    saved_pct = (saved / state_count * 100) if state_count else 0

                    deltas = entity_attr_deltas.get(attr_name, [])
                    num_vals = entity_attr_num_values.get(attr_name, set())
                    if deltas and num_vals:
                        avg_d = sum(deltas) / len(deltas)
                        max_d = max(deltas)
                        min_v = min(num_vals)
                        max_v = max(num_vals)
                        span_v = max_v - min_v

                        # Determine step / quantization
                        sorted_v = sorted(list(num_vals))
                        steps = [round(abs(sorted_v[i] - sorted_v[i-1]), 4) for i in range(1, len(sorted_v))]
                        steps = [s for s in steps if s > 0]
                        min_step = min(steps) if steps else 0

                        sample_distinct_str = ", ".join([str(x) for x in sorted_v[:6]]) + ("..." if len(sorted_v) > 6 else "")
                        print(f"        - {attr_name:<20} changed {occurrences:>7,}x | Alone: -{saved:>6,} ({saved_pct:4.1f}%)")
                        print(f"            Range: [{min_v} .. {max_v}] (Span: {span_v}) | Quantization: {min_step} steps | Samples: [{sample_distinct_str}]")
                        print(f"            Jitter: avg delta {avg_d:.2f}, max delta {max_d:.2f} -> Rule Suggestion: 'tolerance: {avg_d * 1.5:.2f}'")
                    else:
                        samples = ", ".join(list(entity_attr_samples.get(attr_name, []))[:3])
                        metric_str = f"samples: [{samples}]" if samples else "discrete"
                        print(f"        - {attr_name:<20} changed {occurrences:>7,}x | Alone: -{saved:>6,} ({saved_pct:4.1f}%) | {metric_str} -> Rule Suggestion: 'ignore / last'")

                print("      Cumulative potential reduction:")
                for step_idx, (attr_name, cum_saved) in enumerate(cumulative_reductions, start=1):
                    cum_pct = (cum_saved / state_count * 100) if state_count else 0
                    attrs_combined = ", ".join(top_attrs[:step_idx])
                    print(f"        [Top {step_idx}: {attrs_combined}] -> {cum_saved:>7,} rows deleted ({cum_pct:5.1f}%)")
            else:
                print("      No attribute variances detected across duplicate states.")

        print("\n" + "=" * 90)
        print(f" GLOBAL SUMMARY ({len(ranked_entities)} analyzed entities)")
        print("=" * 90)
        print(f" Total states analyzed:                {total_analyzed_states:,}")
        base_global_pct = (total_baseline_deletable / total_analyzed_states * 100) if total_analyzed_states else 0
        print(f" Baseline exact duplicates:           {total_baseline_deletable:,} ({base_global_pct:.1f}% reduction)")

        global_top = global_attr_counter.most_common(top_attributes)
        if global_top:
            print(f"\n Most frequent changing attributes across all analyzed entities:")
            for attr_name, occ in global_top:
                print(f"   {attr_name:<25} {occ:>10,} occurrences")

        print("=" * 90 + "\n")

        return {
            "total_analyzed_states": total_analyzed_states,
            "total_baseline_deletable": total_baseline_deletable,
            "global_top_attributes": global_top,
            "entity_summaries": entity_summaries,
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="HomeAssistant-SmartCompress - Intelligent Database Compression & Optimization for Home Assistant")
    parser.add_argument(
        "config_file",
        nargs="?",
        default=None,
        help="Path to configuration file, e.g. config.postgres.json, config.sqlite.json, or configuration.yaml (optional)",
    )
    parser.add_argument("--level", "-l", default=None, help="Force explicit compression level (e.g. 1, 2, 3 or conservative, moderate, aggressive)")
    parser.add_argument("--entity", "-e", default=None, help="Target specific entity ID or wildcard (e.g. climate.living_room, sensor.power_*)")
    parser.add_argument("--simulate", "-s", action="store_true", help="Simulate and compare all compression settings (read-only)")
    parser.add_argument("--details", "-d", action="store_true", help="Show detailed entity-level breakdown in simulation report")
    parser.add_argument("--limit-entities", type=int, default=None, help="Limit simulation to top N entities")
    parser.add_argument("--analyze", "-a", action="store_true", help="Run in attribute variance analysis mode (read-only)")
    parser.add_argument("--top-entities", type=int, default=None, help="Number of top entities to analyze")
    parser.add_argument("--top-attributes", type=int, default=None, help="Number of top changing attributes to report")
    parser.add_argument(
        "--purge-attributes",
        action="store_true",
        help="Perform a full sweep to purge all unreferenced state_attributes after compression",
    )
    parser.add_argument(
        "--purge-attributes-only",
        action="store_true",
        help="Perform a full sweep to purge all unreferenced state_attributes without compressing states",
    )

    args = parser.parse_args()

    cfg = load_config(args.config_file)

    if args.level:
        cfg["compression_level"] = args.level
        cfg["compression_tiers"] = None

    sample_limit = args.limit_entities or cfg.get("limit_entities") or cfg.get("sample_size")

    if args.purge_attributes_only:
        run_purge_attributes(config=cfg)
    elif args.simulate or cfg.get("simulation") or cfg.get("simulate"):
        run_simulation(
            config=cfg,
            show_details=args.details or cfg.get("detailed", False),
            limit_entities=sample_limit,
            target_entity=args.entity,
        )
    elif args.analyze or cfg.get("analysis", {}).get("enabled", False):
        run_analysis(
            config=cfg,
            top_entities=args.top_entities or sample_limit,
            top_attributes=args.top_attributes,
            target_entity=args.entity,
        )
    else:
        run_compression(
            config=cfg,
            target_entity=args.entity,
            limit_entities=sample_limit,
            purge_all_attributes=args.purge_attributes,
        )


if __name__ == "__main__":
    main()
