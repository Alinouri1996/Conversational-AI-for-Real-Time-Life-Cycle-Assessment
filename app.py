# app.py
from flask import Flask, render_template, request, jsonify, Response, session
from copy import deepcopy
import pandas as pd
import re
import csv
import os, json, time
from collections import defaultdict
from typing import Dict, Any, List, Optional

# =========================
# Flask & Base Paths
# =========================
app = Flask(__name__)

def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

BASIC_AUTH_USERNAME = os.getenv("BASIC_AUTH_USERNAME", "").strip()
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "").strip()
BASIC_AUTH_ENABLED = _env_flag(
    "BASIC_AUTH_ENABLED",
    default=bool(BASIC_AUTH_USERNAME or BASIC_AUTH_PASSWORD),
)

if BASIC_AUTH_ENABLED and (not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD):
    raise RuntimeError(
        "Basic auth is enabled but BASIC_AUTH_USERNAME/BASIC_AUTH_PASSWORD are not both set."
    )

def check_auth(username, password):
    return (
        BASIC_AUTH_ENABLED
        and username == BASIC_AUTH_USERNAME
        and password == BASIC_AUTH_PASSWORD
    )

def authenticate():
    return Response(
        "Login required", 401,
        {"WWW-Authenticate": 'Basic realm="Login Required"'}
    )

@app.before_request
def require_login():
    if not BASIC_AUTH_ENABLED:
        return None
    auth = request.authorization
    if not auth or not check_auth(auth.username, auth.password):
        return authenticate()
    
# Secrets & session
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env_flag("SESSION_COOKIE_SECURE", default=not _env_flag("FLASK_DEBUG", False)),
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ELECTRICITY_DIR = os.path.join(BASE_DIR, "data", "Electricity")
LCA_INPUTS_DIR   = os.path.join(BASE_DIR, "data")
DEFAULT_ENERGY_MIX_FILE = os.path.join(LCA_INPUTS_DIR, "default_energy_mix.csv")
DEFAULT_LCA_INPUT_FILE = os.path.join(LCA_INPUTS_DIR, "default_lca_input.csv")
DEFAULT_OUTPUT_FILE = os.path.join(LCA_INPUTS_DIR, "default_output.csv")

# =========================
# Constants (READ-ONLY defaults)
# =========================
ELECTRICITY_INPUT_NAMES = {
    "biomass": "Biomass.csv",
    "coal": "Coal.csv",
    "diesel_oil": "Diesel and Oil.csv",
    "geothermal": "Geothermal.csv",
    "hydro": "Hydro.csv",
    "gas": "Natural Gas.csv",
    "nuclear": "Nuclear.csv",
    "solar": "Solar.csv",
    "wind": "Wind.csv"
}
TRANSPORT_DATASET_FILE = "A2 - Transport, freight, lorry 16-32 metric ton, euro5 {RoW}.csv"

def _normalize_lookup_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()

def _tokenize_lookup_text(text: str) -> set[str]:
    return set(_normalize_lookup_text(text).split())

def _list_lca_source_files(base_folder: str) -> List[str]:
    return sorted(
        name for name in os.listdir(base_folder)
        if name.lower().endswith(".csv") and not name.lower().startswith("default_")
    )

def _file_match_tokens(file_name: str) -> set[str]:
    stem = os.path.splitext(file_name)[0]
    tokens = _tokenize_lookup_text(stem)
    alias_map = {
        "A1 - Wood Raw Material.csv": {"wood", "raw", "material", "biogenic", "carbon"},
        "A3_H2O.csv": {"h2o", "water"},
        "A3_NaOH.csv": {"naoh", "sodium", "hydroxide"},
        "A3_Natural Gas.csv": {"natural", "gas"},
        "A3_MP.csv": {"mp"},
        TRANSPORT_DATASET_FILE: {"transport", "transportation", "freight", "lorry"},
    }
    return tokens | alias_map.get(file_name, set())

def infer_lca_file_name(process_name: str, base_folder: str) -> str:
    normalized = _normalize_lookup_text(process_name)

    keyword_rules = [
        (("electricity",), "Electricity"),
        (("wood", "raw", "material"), "A1 - Wood Raw Material.csv"),
        (("biogenic", "carbon"), "A1 - Wood Raw Material.csv"),
        (("transport",), TRANSPORT_DATASET_FILE),
        (("natural", "gas"), "A3_Natural Gas.csv"),
        (("naoh",), "A3_NaOH.csv"),
        (("sodium", "hydroxide"), "A3_NaOH.csv"),
        (("h2o",), "A3_H2O.csv"),
        (("water",), "A3_H2O.csv"),
    ]
    for keywords, file_name in keyword_rules:
        if all(keyword in normalized for keyword in keywords):
            return file_name

    if normalized.endswith(" mp") or " mp " in f" {normalized} ":
        return "A3_MP.csv"

    process_tokens = _tokenize_lookup_text(process_name)
    best_file = None
    best_score = -1
    for file_name in _list_lca_source_files(base_folder):
        file_tokens = _file_match_tokens(file_name)
        overlap = len(process_tokens & file_tokens)
        score = overlap * 10

        normalized_file = _normalize_lookup_text(os.path.splitext(file_name)[0])
        if normalized_file and normalized_file in normalized:
            score += 5
        if file_name.startswith("A3_") and normalized.startswith("a3"):
            score += 1

        if score > best_score:
            best_score = score
            best_file = file_name

    if not best_file or best_score <= 0:
        raise ValueError(f"Could not infer a matching data file for process '{process_name}'")
    return best_file

def _load_default_energy_mix(path: str) -> Dict[str, float]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Default energy mix file not found: {path}")

    mix: Dict[str, float] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"source", "percentage"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path} must contain columns: source, percentage")

        for row in reader:
            source = str(row.get("source", "")).strip()
            if not source:
                continue
            if source not in ELECTRICITY_INPUT_NAMES:
                raise ValueError(f"Unknown energy source '{source}' in {path}")
            mix[source] = float(row.get("percentage", 0))

    if not mix:
        raise ValueError(f"{path} does not contain any energy mix rows")
    return mix

def _load_default_lca_input(path: str, base_folder: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Default LCA input file not found: {path}")

    inputs: Dict[str, Dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"process", "module", "amount", "unit"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path} must contain columns: process, module, amount, unit")

        for row in reader:
            process = str(row.get("process", "")).strip()
            if not process:
                continue

            file_name = infer_lca_file_name(process, base_folder)
            explicit_file_name = str(row.get("file_name", "")).strip()
            if explicit_file_name and explicit_file_name != file_name:
                raise ValueError(
                    f"Inferred file '{file_name}' for process '{process}' does not match CSV file_name '{explicit_file_name}'"
                )
            if file_name and file_name.lower() != "electricity":
                file_path = os.path.join(base_folder, file_name)
                if not os.path.exists(file_path):
                    raise FileNotFoundError(f"LCA input file referenced by {path} was not found: {file_path}")

            inputs[process] = {
                "module": str(row.get("module", "")).strip(),
                "Amount": float(row.get("amount", 0)),
                "Unit": str(row.get("unit", "")).strip(),
                "file_name": file_name,
            }

    if not inputs:
        raise ValueError(f"{path} does not contain any LCA input rows")
    return inputs

def _load_default_output(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Default output file not found: {path}")

    outputs: Dict[str, Dict[str, Any]] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"name", "amount", "unit"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError(f"{path} must contain columns: name, amount, unit")

        for row in reader:
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            outputs[name] = {
                "Amount": float(row.get("amount", 0)),
                "Unit": str(row.get("unit", "")).strip(),
            }

    if not outputs:
        raise ValueError(f"{path} does not contain any output rows")
    return outputs

DEFAULT_ENERGY_MIX = _load_default_energy_mix(DEFAULT_ENERGY_MIX_FILE)
ENERGY_KEYS = list(DEFAULT_ENERGY_MIX.keys())

DEFAULT_LCA_INPUT = _load_default_lca_input(DEFAULT_LCA_INPUT_FILE, LCA_INPUTS_DIR)
DEFAULT_OUTPUT = _load_default_output(DEFAULT_OUTPUT_FILE)

# =========================
# Per-user state helpers (session-backed)
# =========================
def get_energy_mix() -> Dict[str, float]:
    if "energy_mix" not in session:
        session["energy_mix"] = deepcopy(DEFAULT_ENERGY_MIX)
    return deepcopy(session["energy_mix"])

def set_energy_mix(mix: Dict[str, float]) -> None:
    session["energy_mix"] = deepcopy(mix)

def get_lca_input() -> Dict[str, Dict[str, Any]]:
    if "lca_input" not in session:
        session["lca_input"] = deepcopy(DEFAULT_LCA_INPUT)
    return deepcopy(session["lca_input"])

def set_lca_input(linp: Dict[str, Dict[str, Any]]) -> None:
    session["lca_input"] = deepcopy(linp)

def get_output() -> Dict[str, Any]:
    return deepcopy(DEFAULT_OUTPUT)

# =========================
# Core LCA Helpers
# =========================
def _safe_read_csv(path: str) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except Exception as e:
        print(f"⚠️ Error reading {path}: {e}")
        return pd.DataFrame()

def generate_energy_mix(folder_path: str, mix_pct: Dict[str, float]) -> pd.DataFrame:
    frames = []
    for source, pct in mix_pct.items():
        fname = ELECTRICITY_INPUT_NAMES.get(source)
        if not fname:
            print(f"Warning: unknown energy source '{source}', skipping.")
            continue
        fpath = os.path.join(folder_path, fname)
        if not os.path.exists(fpath):
            print(f"Warning: electricity file not found: {fpath}")
            continue
        df = _safe_read_csv(fpath)
        if df.empty:
            continue
        df = df[['Impact category', 'Unit', 'Total']].copy()
        df['Weighted Total'] = df['Total'] * (float(pct) / 100.0)
        frames.append(df[['Impact category', 'Unit', 'Weighted Total']])

    if not frames:
        return pd.DataFrame(columns=['Impact category', 'Unit', 'Total'])

    result_df = frames[0].copy()
    for df in frames[1:]:
        result_df = result_df.merge(df, on=['Impact category', 'Unit'], how='outer', suffixes=('', '_x'))
        wcols = [c for c in result_df.columns if c.startswith('Weighted Total')]
        result_df['Weighted Total'] = result_df[wcols].sum(axis=1)
        for c in wcols:
            if c != 'Weighted Total':
                result_df.drop(columns=c, inplace=True)

    result_df.rename(columns={'Weighted Total': 'Total'}, inplace=True)
    return result_df

def calculate_total_lca(folder_path: str, lca_in: Dict[str, Any], elec_mix_df: pd.DataFrame, output: Dict[str, Any]) -> pd.DataFrame:
    module_frames = defaultdict(list)
    for _, info in lca_in.items():
        module = info["module"]
        amount = float(info["Amount"])
        file_name = info["file_name"]

        if str(file_name).lower() == "electricity":
            df = elec_mix_df.copy()
        else:
            fpath = os.path.join(folder_path, file_name)
            if not os.path.exists(fpath):
                print(f"⚠️ File not found: {fpath}")
                continue
            df = _safe_read_csv(fpath)
        if df.empty:
            continue
        df = df[['Impact category', 'Unit', 'Total']].copy()
        df['Total'] = df['Total'] * amount
        df.rename(columns={'Total': module}, inplace=True)
        module_frames[module].append(df)

    merged = None
    for module, dfs in module_frames.items():
        combined = pd.concat(dfs).groupby(['Impact category', 'Unit'], as_index=False).sum()
        merged = combined if merged is None else pd.merge(merged, combined, on=['Impact category', 'Unit'], how='outer')

    if merged is None:
        return pd.DataFrame()

    merged.fillna(0, inplace=True)

    out_amount = next(iter(output.values()))['Amount']
    for col in [c for c in merged.columns if c not in ('Impact category', 'Unit')]:
        merged[col] = merged[col] / out_amount

    return merged

# =========================
# OpenAI Client (env-based)
# =========================
from openai import OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1]
        if "```" in s:
            s = s.rsplit("```", 1)[0]
    return s.strip()

def _safe_json_load(s: str):
    try:
        return json.loads(s)
    except Exception:
        try:
            return json.loads(s.replace("'", '"'))
        except Exception:
            return None

# =========================
# INTENT 1: Update Energy Mix (AI-only)
# =========================
ENERGY_INTENT_CLASSIFIER_PROMPT = (
    "You classify if the user wants to UPDATE the electricity energy mix. "
    "Answer ONLY this JSON: {\"is_update_energy_mix\": true|false, \"rationale\": \"<short>\"}. Return JSON only."
)

def build_energy_mix_extractor_prompt(user_text: str) -> List[Dict[str,str]]:
    schema = {
        "intent": "set_energy_mix",
        "status": "complete|incomplete",
        "missing_fields": [],
        "energy_mix": {k: 0.0 for k in ENERGY_KEYS},
        "message": ""
    }
    system = (
        "You extract electricity energy mix shares for an LCA tool.\n"
        f"Required keys: {', '.join(ENERGY_KEYS)}.\n"
        "Return ONLY a JSON object with exactly these keys: "
        f"{list(schema.keys())}. "
        "Rules:\n"
        "1) Extract percentages as floats.\n"
        "2) If ANY required key is not provided, set status='incomplete', list missing_fields, and DO NOT guess or normalize.\n"
        "3) If all 8 present, set status='complete'.\n"
        "4) Include a short 'message'; if incomplete, include a concrete example.\n"
        "5) JSON only. No code fences."
    )
    few_shot = [
        {
            "role": "user",
            "content": "Set 30 solar, 25 wind, 20 gas, 10 coal, 5 nuclear, 5 hydro, 3 biomass, 2 diesel oil."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "intent": "set_energy_mix",
                "status": "complete",
                "missing_fields": [],
                "energy_mix": {
                    "biomass": 3.0, "coal": 10.0, "solar": 30.0, "wind": 25.0,
                    "diesel_oil": 2.0, "gas": 20.0, "nuclear": 5.0, "hydro": 5.0
                },
                "message": "Parsed all 8 sources."
            })
        },
        {
            "role": "user",
            "content": "Make it 50 solar and 50 wind."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "intent": "set_energy_mix",
                "status": "incomplete",
                "missing_fields": ["biomass","coal","diesel_oil","gas","nuclear","hydro"],
                "energy_mix": {
                    "biomass": 0.0, "coal": 0.0, "solar": 50.0, "wind": 50.0,
                    "diesel_oil": 0.0, "gas": 0.0, "nuclear": 0.0, "hydro": 0.0
                },
                "message": "Please provide all 8 sources. Example: 'solar 30, wind 25, gas 20, coal 10, nuclear 5, hydro 5, biomass 3, diesel_oil 2'."
            })
        }
    ]
    return [
        {"role": "system", "content": system},
        *few_shot,
        {"role": "user", "content": user_text}
    ]

def ai_update_energy_mix(user_text: str) -> Dict[str, Any]:
    if client is None:
        return {"intent":"set_energy_mix","ok":False,"needs_more":True,"data":None,"errors":["OPENAI_API_KEY not set"],"message":"LLM is not configured."}

    # classify
    resp = client.chat.completions.create(
        model="gpt-4o", temperature=0.0,
        messages=[{"role":"system","content":ENERGY_INTENT_CLASSIFIER_PROMPT},{"role":"user","content":user_text}]
    )
    j = _safe_json_load(_strip_fences(resp.choices[0].message.content))
    if not j or not j.get("is_update_energy_mix", False):
        return {"intent":"set_energy_mix","ok":False,"needs_more":False,"data":None,"errors":["Not an energy mix update"],"message":"This does not look like an energy mix change."}

    # extract
    resp2 = client.chat.completions.create(
        model="gpt-4o", temperature=0.0,
        messages=build_energy_mix_extractor_prompt(user_text)
    )
    data = _safe_json_load(_strip_fences(resp2.choices[0].message.content))
    if not data or data.get("intent") != "set_energy_mix":
        return {"intent":"set_energy_mix","ok":False,"needs_more":True,"data":None,"errors":["Parse failure"],"message":"Couldn’t parse energy mix; provide all 8 sources."}

    status = data.get("status","incomplete")
    mix = data.get("energy_mix", {})
    missing = data.get("missing_fields", []) or []
    msg = data.get("message","")
    ok = (status == "complete") and all(k in mix for k in ENERGY_KEYS)

    return {
        "intent":"set_energy_mix",
        "ok": ok,
        "needs_more": not ok,
        "data": mix if ok else None,
        "errors": ([] if ok else [f"Missing: {', '.join(missing)}"]),
        "message": msg
    }

# =========================
# INTENT 2: Update Input Amount(s) (AI-only)
# =========================
INPUTS_INTENT_CLASSIFIER_PROMPT = (
    "You classify if the user wants to UPDATE one or more LCA input amounts. "
    "Answer ONLY this JSON: {\"is_update_inputs\": true|false, \"rationale\": \"<short>\"}. Return JSON only."
)

def build_input_updates_extractor_prompt(user_text: str, valid_keys: List[str]) -> List[Dict[str,str]]:
    schema = {
        "intent": "update_input_amount",
        "updates": [{"key":"<ONE OF valid_keys>","amount":0.0,"unit":"kWh|tkm|MJ|kg|..."}],
        "unmatched": [],
        "message": ""
    }
    system = (
        "You extract updates to LCA input amounts. "
        "Keys MUST be chosen ONLY from valid_keys. Others go to 'unmatched'. "
        "Amounts must be numeric floats; units are free text.\n"
        f"valid_keys: {valid_keys}\n"
        "Return ONLY JSON with keys: " + ", ".join(schema.keys()) + "."
    )
    few_shot = [
        {
            "role":"user",
            "content":"Increase A3 - Pressing - Electricity to 500000 kWh and set A2 - Transportatoin to 600 tkm."
        },
        {
            "role":"assistant",
            "content": json.dumps({
                "intent": "update_input_amount",
                "updates": [
                    {"key": "A3 - Pressing - Electricity", "amount": 500000.0, "unit": "kWh"},
                    {"key": "A2 - Transportatoin", "amount": 600.0, "unit": "tkm"}
                ],
                "unmatched": [],
                "message": "Two updates parsed."
            })
        },
        {
            "role":"user",
            "content":"Set pressing electricity to 300000 and add Cooling stage to 20 MJ."
        },
        {
            "role":"assistant",
            "content": json.dumps({
                "intent": "update_input_amount",
                "updates": [
                    {"key": "A3 - Pressing - Electricity", "amount": 300000.0, "unit": ""}
                ],
                "unmatched": ["Cooling stage"],
                "message": "One valid update; 'Cooling stage' not found in valid_keys."
            })
        }
    ]
    return [{"role":"system","content":system}, *few_shot, {"role":"user","content":user_text}]

def ai_update_input_amounts(user_text: str, lca_input: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if client is None:
        return {"intent":"update_input_amount","ok":False,"needs_more":True,"data":None,"errors":["OPENAI_API_KEY not set"],"message":"LLM is not configured."}

    valid_keys = list(lca_input.keys())

    # classify
    resp = client.chat.completions.create(
        model="gpt-4o", temperature=0.0,
        messages=[{"role":"system","content":INPUTS_INTENT_CLASSIFIER_PROMPT},{"role":"user","content":user_text}]
    )
    j = _safe_json_load(_strip_fences(resp.choices[0].message.content))
    if not j or not j.get("is_update_inputs", False):
        return {"intent":"update_input_amount","ok":False,"needs_more":False,"data":None,"errors":["Not an inputs update"],"message":"This doesn’t look like an input change."}

    # extract
    resp2 = client.chat.completions.create(
        model="gpt-4o", temperature=0.0,
        messages=build_input_updates_extractor_prompt(user_text, valid_keys)
    )
    data = _safe_json_load(_strip_fences(resp2.choices[0].message.content))
    if not data or data.get("intent") != "update_input_amount":
        return {"intent":"update_input_amount","ok":False,"needs_more":True,"data":None,"errors":["Parse failure"],"message":"Couldn’t parse any updates. Please specify exact process key and amount."}

    updates = data.get("updates", []) or []
    unmatched = data.get("unmatched", []) or []
    msg = data.get("message","").strip()

    ok = len(updates) > 0
    errs = []
    if unmatched:
        errs.append(f"Unrecognized items: {', '.join(unmatched)}")
    if not ok:
        errs.append("No valid updates parsed.")

    return {
        "intent":"update_input_amount",
        "ok": ok,
        "needs_more": not ok,
        "data": updates if ok else None,
        "errors": errs,
        "message": msg or ("Parsed updates." if ok else "Please use an exact key from the list shown in the UI.")
    }

# =========================
# INTENT 3: Compare Scenarios (AI-only)
# =========================
SCENARIO_INTENT_CLASSIFIER_PROMPT = (
    "You classify if the user wants to COMPARE two or more scenarios. "
    "Answer ONLY this JSON: {\"is_compare_scenarios\": true|false, \"rationale\": \"<short>\"}. Return JSON only."
)

def build_compare_scenarios_extractor_prompt(user_text: str, energy_keys: List[str], valid_keys: List[str], default_energy: Dict[str, float]) -> List[Dict[str,str]]:
    schema = {
        "intent": "compare_scenarios",
        "status": "complete|incomplete",
        "message": "",
        "invalid_scenarios": [],
        "scenarios": [
            {
                "name": "",
                "use_default_energy_mix": True,
                "energy_mix": {k: 0.0 for k in energy_keys},
                "input_updates": [{"key":"<ONE OF valid_keys>","amount":0.0,"unit":"kWh|tkm|MJ|kg|..."}]
            }
        ]
    }
    rules = (
        "You extract SCENARIOS for an LCA tool.\n"
        "- There must be at least TWO scenarios.\n"
        f"- If a scenario mentions an energy mix, it MUST include ALL of: {', '.join(energy_keys)}.\n"
        "- If a scenario omits energy mix, set use_default_energy_mix=true.\n"
        "- For input updates, keys MUST come from valid_keys only; others belong in invalid_scenarios with an error.\n"
        "- Never invent values. Never normalize. Use floats for numbers.\n"
        "- Return ONLY a JSON object with keys exactly: " + ", ".join(schema.keys()) + ".\n"
        "- If any scenario is invalid (e.g., partial energy mix), set status='incomplete', keep the scenario, and list errors in invalid_scenarios. Provide a short 'message'."
    )
    few_shot = [
        {
            "role": "user",
            "content": "Compare Baseline vs Green: Baseline keeps defaults. Green uses 40 solar, 30 wind, 10 gas, 10 hydro, 5 nuclear, 3 biomass, 1 coal, 1 diesel oil; cut A3 - Pressing - Electricity to 600000 kWh."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "intent": "compare_scenarios",
                "status": "complete",
                "message": "Two scenarios parsed.",
                "invalid_scenarios": [],
                "scenarios": [
                    {"name":"Baseline","use_default_energy_mix":True,"energy_mix":{k:0.0 for k in energy_keys},"input_updates":[]},
                    {"name":"Green","use_default_energy_mix":False,"energy_mix":{"solar":40.0,"wind":30.0,"gas":10.0,"hydro":10.0,"nuclear":5.0,"biomass":3.0,"coal":1.0,"diesel_oil":1.0},
                     "input_updates":[{"key":"A3 - Pressing - Electricity","amount":600000.0,"unit":"kWh"}]}
                ]
            })
        },
        {
            "role": "user",
            "content": "Compare S1 vs S2: S1 has 50 solar, 50 wind. S2 increases A4 - Transportatoin to 500000 tkm."
        },
        {
            "role": "assistant",
            "content": json.dumps({
                "intent": "compare_scenarios",
                "status": "incomplete",
                "message": "S1 energy mix missing: biomass, coal, diesel_oil, gas, nuclear, hydro. Provide all 8 values.",
                "invalid_scenarios": [
                    {"name": "S1", "errors": ["Energy mix is partial; missing: biomass, coal, diesel_oil, gas, nuclear, hydro."]}
                ],
                "scenarios": [
                    {"name":"S1","use_default_energy_mix":False,
                     "energy_mix":{"solar":50.0,"wind":50.0,"biomass":0.0,"coal":0.0,"diesel_oil":0.0,"gas":0.0,"nuclear":0.0,"hydro":0.0},
                     "input_updates":[]},
                    {"name":"S2","use_default_energy_mix":True,"energy_mix":{k:0.0 for k in energy_keys},
                     "input_updates":[{"key":"A4 - Transportatoin","amount":500000.0,"unit":"tkm"}]}
                ]
            })
        }
    ]
    system = (
        rules
        + "\nvalid_keys:\n- " + "\n- ".join(valid_keys)
        + "\nDefault energy mix (reference; don’t auto-fill unless 'use_default_energy_mix' is true):\n"
        + json.dumps(default_energy)
    )
    return [{"role": "system", "content": system}, *few_shot, {"role":"user","content":user_text}]

def ai_compare_scenarios(user_text: str, default_energy_mix: dict, lca_input: dict) -> dict:
    if client is None:
        return {"intent":"compare_scenarios","ok":False,"needs_more":True,"data":None,"errors":["OPENAI_API_KEY not set"],"message":"LLM is not configured."}

    # classify
    resp = client.chat.completions.create(
        model="gpt-4o", temperature=0.0,
        messages=[{"role":"system","content":SCENARIO_INTENT_CLASSIFIER_PROMPT},{"role":"user","content":user_text}]
    )
    j = _safe_json_load(_strip_fences(resp.choices[0].message.content))
    if not j or not j.get("is_compare_scenarios", False):
        return {"intent":"compare_scenarios","ok":False,"needs_more":False,"data":None,"errors":["Not a compare-scenarios request"],"message":"This doesn’t look like a scenario comparison."}

    # extract
    valid_keys = list(lca_input.keys())
    resp2 = client.chat.completions.create(
        model="gpt-4o", temperature=0.0,
        messages=build_compare_scenarios_extractor_prompt(user_text, ENERGY_KEYS, valid_keys, default_energy_mix)
    )
    data = _safe_json_load(_strip_fences(resp2.choices[0].message.content))
    if not data or data.get("intent") != "compare_scenarios":
        return {"intent":"compare_scenarios","ok":False,"needs_more":True,"data":None,"errors":["Parse failure"],"message":"Couldn’t parse scenarios. Provide at least two named scenarios."}

    status = data.get("status","incomplete")
    invalid = data.get("invalid_scenarios", []) or []
    scenarios = data.get("scenarios", []) or []
    msg = data.get("message","").strip()

    errors = []
    if len(scenarios) < 2:
        errors.append("Need at least two scenarios.")
    for entry in invalid:
        nm = entry.get("name","Unnamed")
        for e in entry.get("errors", []):
            errors.append(f"[{nm}] {e}")

    ok = (len(errors) == 0 and status == "complete")
    return {
        "intent":"compare_scenarios",
        "ok": ok,
        "needs_more": not ok,
        "data": ({"scenarios": scenarios} if ok else None),
        "errors": errors,
        "message": msg if msg else ("Scenarios parsed." if ok else "Please fix the highlighted issues.")
    }

# =========================
# Presentation helpers
# =========================
def apply_input_updates(base: Dict[str, Dict[str, Any]], updates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Return a COPY of lca_input with the updates applied."""
    new_inp = {k: dict(v) for k, v in base.items()}
    for u in updates or []:
        key = u.get("key")
        if key in new_inp:
            try:
                amt = float(u.get("amount"))
                new_inp[key]["Amount"] = amt
            except Exception:
                pass
    return new_inp

def format_df_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    ordered_cols = ["Impact category", "Unit"] + [c for c in df.columns if c not in ["Impact category", "Unit"]]
    df = df[ordered_cols]
    def fmt(x):
        return f"{x:.2e}" if isinstance(x, (int, float, float)) else x
    return df.applymap(fmt).to_dict(orient="records")

def summarize_lca(df: pd.DataFrame) -> dict:
    """Return simple totals by indicator and by module (floats → strings)."""
    if df is None or df.empty:
        return {"totals_by_indicator": {}, "totals_by_module": {}}
    mod_cols = [c for c in df.columns if c not in ("Impact category", "Unit")]
    by_indicator = (df.set_index("Impact category")[mod_cols].sum(axis=1)).to_dict()
    by_module = df[mod_cols].sum(axis=0).to_dict()
    fmt = lambda v: f"{v:.2e}"
    return {
        "totals_by_indicator": {k: fmt(v) for k, v in by_indicator.items()},
        "totals_by_module":    {k: fmt(v) for k, v in by_module.items()},
    }

# (A few advanced helpers from your original file omitted for brevity in this section)
# You can keep your _top5_indicators, ai_present_results, build_payload_for_complexity, etc., unchanged below.
# I leave them intact so your chatbot presentation still works:

# ---- Keep your advanced indicator helpers as-is (BEGIN) ----
_PREFERRED_TOP5 = [
    {"canonical": "Climate change",            "patterns": [r"\bclimate\s*change\b", r"\bgwp\b"]},
    {"canonical": "Acidification",             "patterns": [r"\bacidification\b", r"\bsap\b"]},
    {"canonical": "Ozone depletion",           "patterns": [r"\bozone\s*depletion\b", r"\bodp\b"]},
    {"canonical": "Eutrophication, freshwater","patterns": [r"\beutrophication[, ]*\s*fresh\s*water\b",
                                                            r"\befp\b", r"\bfeu\b", r"\beutrophication.*fresh"]},
    {"canonical": "Non-renewable, fossil",     "patterns": [r"\bnon[-\s]*renewable.*fossil\b",
                                                            r"\bfossil\s*resource\b", r"\bfossil\s*depletion\b",
                                                            r"\bnrd\b", r"\bfdp\b"]},
]

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def _match_indicator_label(available: List[str], patterns: List[str]) -> Optional[str]:
    import re as _re
    for pat in patterns:
        rx = _re.compile(pat, flags=_re.IGNORECASE)
        for label in available:
            if rx.search(label):
                return label
    return None

def _numeric_totals_by_indicator(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {}
    mod_cols = [c for c in df.columns if c not in ("Impact category", "Unit")]
    return (df.set_index("Impact category")[mod_cols].sum(axis=1)).to_dict()

def _top5_indicators(df: pd.DataFrame) -> List[tuple]:
    if df is None or df.empty:
        return []
    totals = _numeric_totals_by_indicator(df)
    available_labels = list(totals.keys())
    selected: List[tuple] = []
    used = set()
    for pref in _PREFERRED_TOP5:
        label = _match_indicator_label(available_labels, pref["patterns"])
        if label is not None and label not in used:
            selected.append((label, totals.get(label, 0.0)))
            used.add(label)
    if len(selected) < 5:
        remaining = [(lbl, val) for lbl, val in totals.items() if lbl not in used]
        remaining.sort(key=lambda kv: kv[1], reverse=True)
        selected.extend(remaining[: 5 - len(selected)])
    return selected[:5]

def _find_gwp_label(df: pd.DataFrame) -> Optional[str]:
    names = set(df["Impact category"].astype(str))
    for target in ["Climate change total", "GWP-total", "Climate change", "GWP (total)", "GWP"]:
        if target in names:
            return target
    for n in names:
        u = n.upper()
        if "GWP" in u or "CLIMATE" in u:
            return n
    return None

def _per_indicator_module_breakdown(df: pd.DataFrame) -> dict:
    out = {}
    if df is None or df.empty:
        return out
    for _, row in df.iterrows():
        ind = str(row["Impact category"])
        out[ind] = {}
        for k, v in row.items():
            if k in ("Impact category", "Unit"):
                continue
            try:
                out[ind][k] = float(v)
            except Exception:
                pass
    return out

def _indicator_units(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {}
    return df.groupby("Impact category").first()["Unit"].astype(str).to_dict()

def ai_present_results(
    complexity: int,
    df: pd.DataFrame,
    *,
    intent: str = "",
    scenario_name: Optional[str] = None,
    energy_mix_used: Optional[str] = None,
    notes: Optional[dict] = None
) -> dict:
    if client is None:
        return {"markdown": "Results available. (Set OPENAI_API_KEY to enable tailored explanations.)"}

    units_by_indicator = _indicator_units(df)
    totals_numeric = _numeric_totals_by_indicator(df)
    top5 = _top5_indicators(df)
    per_indicator_modules = _per_indicator_module_breakdown(df)
    gwp_label = _find_gwp_label(df)

    payload = {
        "intent": intent,
        "scenario_name": scenario_name or "",
        "energy_mix_used": energy_mix_used or "",
        "gwp_label": gwp_label or "",
        "gwp_unit": units_by_indicator.get(gwp_label, "") if gwp_label else "",
        "indicator_units": units_by_indicator,
        "totals_by_indicator": totals_numeric,
        "top5_indicators": [
            {"name": n, "value": v, "unit": units_by_indicator.get(n, "")} for n, v in top5
        ],
        "per_indicator_modules": per_indicator_modules,
        "module_names": sorted({m for d in per_indicator_modules.values() for m in d.keys()}),
        "notes": notes or {},
    }

    LEVEL1_SYSTEM = (
        "You are an LCA presenter for non-expert audiences.\n"
        "- Markdown only; plain language; show only carbon emissions (GWP) total as 'carbon emissions'.\n"
        "- One short interpretation and one approximate real-world comparison.\n"
        "- No per-module breakdown.\n"
        "- Never invent numbers.\n"
    )
    LEVEL1_USER = "Present a brief friendly result.\nData:\n" + json.dumps(payload)

    LEVEL2_SYSTEM = (
        "You are an LCA presenter for semi-expert audiences.\n"
        "- Markdown only; show top-5 indicators (original labels) with values + short meaning.\n"
        "- Add concise definitions of Climate change, Acidification, Ozone depletion, Eutrophication (freshwater), Non-renewable, fossil.\n"
        "- One real-world comparison for carbon emissions.\n"
        "- No per-module breakdown.\n"
        "- Never invent numbers.\n"
    )
    LEVEL2_USER = "Present a medium-detail result.\nData:\n" + json.dumps(payload)

    LEVEL3_SYSTEM = (
        "You are an LCA presenter for expert audiences.\n"
        "- Markdown only; provide all indicators with values per-module.\n"
        "- Technical but concise.\n"
        "- Add short interpretation of module meanings.\n"
        "- One approximate real-world comparison for carbon emissions.\n"
        "- Never invent numbers.\n"
    )
    LEVEL3_USER = "Present a detailed expert result.\nData:\n" + json.dumps(payload)

    if complexity == 1:
        messages = [{"role": "system", "content": LEVEL1_SYSTEM}, {"role": "user", "content": LEVEL1_USER}]
    elif complexity == 2:
        messages = [{"role": "system", "content": LEVEL2_SYSTEM}, {"role": "user", "content": LEVEL2_USER}]
    else:
        messages = [{"role": "system", "content": LEVEL3_SYSTEM}, {"role": "user", "content": LEVEL3_USER}]

    resp = client.chat.completions.create(model="gpt-4o", temperature=0.2, messages=messages)
    return {"markdown": resp.choices[0].message.content.strip()}

def build_payload_for_complexity(complexity: int, df: pd.DataFrame, *, presentation: dict, summary: Optional[dict] = None):
    data = {"presentation": presentation}
    if complexity >= 2:
        table_rows = format_df_records(df)
        data["table"] = table_rows
        if summary:
            data["summary"] = summary
    return data
# ---- Keep your advanced indicator helpers as-is (END) ----

# =========================
# Routes
# =========================
@app.route('/')
def index():
    return render_template("index.html",
                           energy_mix=get_energy_mix(),
                           lca_input=get_lca_input())

@app.post('/update_energy_mix')
def update_energy_mix_route():
    new_mix = (request.json or {}).get("energy_mix", {}) or {}
    base = get_energy_mix()
    # copy + validate keys
    fixed = {k: float(new_mix.get(k, base.get(k, 0.0))) for k in base.keys()}
    set_energy_mix(fixed)
    _ = generate_energy_mix(ELECTRICITY_DIR, fixed)  # optional warm-up
    return jsonify({"status": "success", "energy_mix": fixed})

@app.post('/update_lca_input')
def update_lca_input_route():
    updated_inputs = (request.json or {}).get("lca_input", {}) or {}
    linp = get_lca_input()
    for key in linp:
        if key in updated_inputs and "Amount" in updated_inputs[key]:
            try:
                linp[key]["Amount"] = float(updated_inputs[key]["Amount"])
            except Exception:
                pass
    set_lca_input(linp)
    mix_df = generate_energy_mix(ELECTRICITY_DIR, get_energy_mix())
    result_df = calculate_total_lca(LCA_INPUTS_DIR, linp, mix_df, get_output())
    return result_df.to_json(orient='records')

@app.post('/compare_scenarios')
def compare_scenarios_route():
    scenarios = (request.json or {}).get("scenarios", {}) or {}
    results = {}
    base_inputs = get_lca_input()
    for scenario_name, data in scenarios.items():
        energy_mix_input = (data or {}).get("energy_mix") or get_energy_mix()
        lca_input_input = (data or {}).get("lca_input") or {}
        try:
            mix_df = generate_energy_mix(ELECTRICITY_DIR, energy_mix_input)
            updated = {k: dict(v) for k, v in base_inputs.items()}
            for key, val in lca_input_input.items():
                if key in updated and "Amount" in val:
                    updated[key]["Amount"] = float(val["Amount"])
            df = calculate_total_lca(LCA_INPUTS_DIR, updated, mix_df, get_output())
            results[scenario_name] = df.to_dict(orient='records')
        except Exception as e:
            results[scenario_name] = {"error": str(e)}
    return jsonify(results)

@app.route('/sensitivity_analysis', methods=['POST'])
def sensitivity_analysis():
    data = request.json or {}
    base_energy_mix = (data.get("base", {}) or {}).get("energy_mix") or get_energy_mix()
    base_lca_input = (data.get("base", {}) or {}).get("lca_input") or get_lca_input()

    variation_percent = 0.2
    results = []

    mix_df = generate_energy_mix(ELECTRICITY_DIR, base_energy_mix)

    full_input = {
        k: {
            "module": v["module"],
            "Amount": float(v["Amount"]),
            "Unit": v["Unit"],
            "file_name": v["file_name"]
        }
        for k, v in base_lca_input.items()
        if all(key in v for key in ("module", "Amount", "Unit", "file_name"))
    }

    if not full_input:
        return jsonify({"error": "All LCA input entries are missing required fields."}), 400

    base_df = calculate_total_lca(LCA_INPUTS_DIR, full_input, mix_df, get_output())
    if base_df is None or base_df.empty:
        return jsonify({"error": "Base LCA calculation failed."}), 500

    base_totals = base_df.set_index("Impact category").drop(columns="Unit").sum(axis=1)

    for process, props in full_input.items():
        varied_input = {k: v.copy() for k, v in full_input.items()}
        original = props["Amount"]
        varied_input[process]["Amount"] = original * (1 + variation_percent)

        varied_df = calculate_total_lca(LCA_INPUTS_DIR, varied_input, mix_df, get_output())
        if varied_df is None or varied_df.empty:
            continue

        varied_totals = varied_df.set_index("Impact category").drop(columns="Unit").sum(axis=1)

        diffs = {}
        for category in base_totals.index:
            base_val = base_totals[category]
            new_val = varied_totals.get(category, base_val)
            change = ((new_val - base_val) / base_val * 100) if base_val != 0 else 0
            diffs[category] = round(change, 2)

        results.append({
            "input": process,
            "variation": diffs
        })

    return jsonify({"response": "sensitivity_analysis", "results": results})

# =========================
# Chatbot (AI → Execute)
# =========================
@app.post("/chatbot")
def chatbot():
    payload = request.json or {}
    text = (payload.get("message") or "").strip()
    complexity = int(payload.get("complexity", 2))

    if not text:
        return jsonify({
            "intent": "general",
            "status": "error",
            "response": "Empty message.",
            "data": None
        }), 400

    def pack(intent, status, response, data=None):
        return jsonify({
            "intent": intent,
            "status": status,           # "ok" | "needs_more" | "error"
            "response": response,       # short text your UI can show directly
            "data": data or {}          # structured payloads
        })

    if client is None:
        return pack(
            "general",
            "ok",
            "I can compare scenarios, update the energy mix, or change specific inputs. "
            "Set OPENAI_API_KEY to enable chatbot intent parsing and free-form responses.",
            {},
        )

    # 1) Try Compare Scenarios first
    cmp_res = ai_compare_scenarios(text, default_energy_mix=get_energy_mix(), lca_input=get_lca_input())
    if cmp_res["ok"] or cmp_res["needs_more"]:
        if not cmp_res["ok"]:
            out = []
            warnings = []
            scenarios = (cmp_res.get("data", {}) or {}).get("scenarios", [])
            for s in scenarios:
                name = s.get("name", "Scenario")
                use_def = bool(s.get("use_default_energy_mix", True))
                mix = get_energy_mix() if use_def else s.get("energy_mix", {})
                valid_mix = use_def or all(k in mix for k in ENERGY_KEYS)
                if not valid_mix:
                    warnings.append(f"[{name}] Energy mix incomplete (needs all 8 sources).")
                    continue
                mix_df = generate_energy_mix(ELECTRICITY_DIR, mix)
                updated_inputs = apply_input_updates(get_lca_input(), s.get("input_updates", []))
                df = calculate_total_lca(LCA_INPUTS_DIR, updated_inputs, mix_df, get_output())
                out.append({"name": name, "energy_mix_used": ("default" if use_def else "custom"), "table": format_df_records(df)})

            example_prompt = (
                "Compare Baseline and Green. Baseline uses default settings. "
                "Green energy mix: solar 30, wind 25, gas 20, coal 10, nuclear 5, hydro 5, biomass 3, diesel_oil 2. "
                "Green input updates: set 'A3 - Pressing - Electricity' to 600000 kWh."
            )
            resp_msg = cmp_res.get("message") or "I identified that you want to compare scenarios, but some details are missing."
            return pack(
                "compare_scenarios",
                "needs_more",
                resp_msg + " Here’s the best way to phrase it, and I’ve proceeded where possible.",
                {"example_prompt": example_prompt, "warnings": warnings, "results": out}
            )

        # OK → run all scenarios and return results
        out = []
        for s in cmp_res["data"]["scenarios"]:
            name = s.get("name","Scenario")
            use_def = bool(s.get("use_default_energy_mix", True))
            mix = get_energy_mix() if use_def else s.get("energy_mix", {})
            mix_df = generate_energy_mix(ELECTRICITY_DIR, mix)
            updated_inputs = apply_input_updates(get_lca_input(), s.get("input_updates", []))
            df = calculate_total_lca(LCA_INPUTS_DIR, updated_inputs, mix_df, get_output())
            presentation = ai_present_results(complexity, df, intent="compare_scenarios", scenario_name=name, energy_mix_used=("default" if use_def else "custom"))
            summ = summarize_lca(df)
            out.append({"name": name, "data": build_payload_for_complexity(complexity, df, presentation=presentation, summary=summ)})
        return pack("compare_scenarios", "ok", "Scenario comparison complete.", {"results": out})

    # 2) Energy Mix Update
    emix = ai_update_energy_mix(text)
    if emix["ok"] or emix["needs_more"]:
        if not emix["ok"]:
            missing = (emix.get("errors") or ["Missing energy sources"])[0]
            example_prompt = "Set energy mix to: solar 30, wind 25, gas 20, coal 10, nuclear 5, hydro 5, biomass 3, diesel_oil 2"
            resp_msg = "I identified you want to update the energy mix, but details are missing."
            return pack(
                "set_energy_mix",
                "needs_more",
                resp_msg + " Provide all 8 sources.",
                {"missing_fields": missing, "example_prompt": example_prompt}
            )

        # OK → compute with provided full mix (does NOT overwrite user session unless you want to)
        new_mix = emix["data"]
        mix_df = generate_energy_mix(ELECTRICITY_DIR, new_mix)
        df = calculate_total_lca(LCA_INPUTS_DIR, get_lca_input(), mix_df, get_output())
        presentation = ai_present_results(complexity, df, intent="set_energy_mix", scenario_name="Updated Energy Mix", energy_mix_used="custom")
        summ = summarize_lca(df)
        return pack(
            "set_energy_mix",
            "ok",
            "Recalculated with the provided energy mix.",
            build_payload_for_complexity(complexity, df, presentation=presentation, summary=summ)
        )

    # 3) Input Amount Updates
    upd = ai_update_input_amounts(text, get_lca_input())
    if upd["ok"] or upd["needs_more"]:
        if not upd["ok"]:
            example_prompt = "Set 'A3 - Pressing - Electricity' to 500000 kWh; set 'A2 - Transportatoin' to 600 tkm"
            resp_msg = upd.get("message") or "I identified you want to update input amounts, but I couldn’t parse valid updates."
            return pack(
                "update_input_amount",
                "needs_more",
                resp_msg + " Here’s the best way to phrase it.",
                {"errors": upd.get("errors") or [], "valid_keys": list(get_lca_input().keys()), "example_prompt": example_prompt}
            )

        # OK → apply parsed updates and compute (does not persist unless you decide to set_lca_input)
        updated_inputs = apply_input_updates(get_lca_input(), upd["data"])
        mix_df = generate_energy_mix(ELECTRICITY_DIR, get_energy_mix())
        df = calculate_total_lca(LCA_INPUTS_DIR, updated_inputs, mix_df, get_output())
        presentation = ai_present_results(complexity, df, intent="update_input_amount", scenario_name="Updated Inputs", energy_mix_used="default")
        summ = summarize_lca(df)
        return pack(
            "update_input_amount",
            "ok",
            "Recalculated with the updated inputs.",
            build_payload_for_complexity(complexity, df, presentation=presentation, summary=summ)
        )

    # 4) Non-intent prompts → friendly LLM answer or fallback
    resp = client.chat.completions.create(
        model="gpt-4o",
        temperature=0.3,
        messages=[
            {"role": "system", "content": "You are an environmental LCA expert. Be concise, specific, and helpful. Use SI units when possible."},
            {"role": "user", "content": text},
        ],
    )
    return pack("general", "ok", resp.choices[0].message.content.strip(), {})

# =========================
# Main
# =========================
if __name__ == "__main__":
    # In production, you'll run via gunicorn; debug only for local dev
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=_env_flag("FLASK_DEBUG", False),
    )
