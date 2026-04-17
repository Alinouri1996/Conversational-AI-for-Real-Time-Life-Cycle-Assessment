import os
import csv
import pandas as pd

# Mapping of energy sources to Excel filenames
electricity_input_names = {
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
ELECTRICITY_DIR = os.path.join(DATA_DIR, "Electricity")
DEFAULT_ENERGY_MIX_FILE = os.path.join(DATA_DIR, "default_energy_mix.csv")
DEFAULT_LCA_INPUT_FILE = os.path.join(DATA_DIR, "default_lca_input.csv")
DEFAULT_OUTPUT_FILE = os.path.join(DATA_DIR, "default_output.csv")

def load_default_energy_mix(path):
    mix = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            source = str(row.get("source", "")).strip()
            if not source:
                continue
            mix[source] = float(row.get("percentage", 0))
    return mix

def normalize_lookup_text(text):
    import re
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()

def tokenize_lookup_text(text):
    return set(normalize_lookup_text(text).split())

def list_lca_source_files(base_folder):
    return sorted(
        name for name in os.listdir(base_folder)
        if name.lower().endswith(".csv") and not name.lower().startswith("default_")
    )

def file_match_tokens(file_name):
    stem = os.path.splitext(file_name)[0]
    tokens = tokenize_lookup_text(stem)
    alias_map = {
        "A1 - Wood Raw Material.csv": {"wood", "raw", "material", "biogenic", "carbon"},
        "A3_H2O.csv": {"h2o", "water"},
        "A3_NaOH.csv": {"naoh", "sodium", "hydroxide"},
        "A3_Natural Gas.csv": {"natural", "gas"},
        "A3_MP.csv": {"mp"},
        TRANSPORT_DATASET_FILE: {"transport", "transportation", "freight", "lorry"},
    }
    return tokens | alias_map.get(file_name, set())

def infer_lca_file_name(process_name, base_folder):
    normalized = normalize_lookup_text(process_name)

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

    process_tokens = tokenize_lookup_text(process_name)
    best_file = None
    best_score = -1
    for file_name in list_lca_source_files(base_folder):
        file_tokens = file_match_tokens(file_name)
        overlap = len(process_tokens & file_tokens)
        score = overlap * 10

        normalized_file = normalize_lookup_text(os.path.splitext(file_name)[0])
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

def load_default_lca_input(path):
    lca_input = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            process = str(row.get("process", "")).strip()
            if not process:
                continue
            file_name = infer_lca_file_name(process, DATA_DIR)
            explicit_file_name = str(row.get("file_name", "")).strip()
            if explicit_file_name and explicit_file_name != file_name:
                raise ValueError(
                    f"Inferred file '{file_name}' for process '{process}' does not match CSV file_name '{explicit_file_name}'"
                )
            lca_input[process] = {
                "module": str(row.get("module", "")).strip(),
                "Amount": float(row.get("amount", 0)),
                "Unit": str(row.get("unit", "")).strip(),
                "file_name": file_name,
            }
    return lca_input

def load_default_output(path):
    output = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            output[name] = {
                "Amount": float(row.get("amount", 0)),
                "Unit": str(row.get("unit", "")).strip(),
            }
    return output

def generate_energy_mix(folder_path, energy_mix_percentages):
    """
    Generate an energy mixture impact profile based on Excel files and percentage shares.

    Parameters:
    - folder_path (str): Path to the folder containing Excel files for each energy source.
    - energy_mix_percentages (dict): Dictionary mapping energy source keys to their percentage share (0–100).

    Returns:
    - pd.DataFrame: DataFrame of total environmental impacts for the mixture per 1 kWh.
    """
    mix_dfs = []

    for source, percentage in energy_mix_percentages.items():
        if source not in electricity_input_names:
            print(f"Warning: '{source}' not found in electricity_input_names mapping. Skipping.")
            continue

        file_name = electricity_input_names[source]
        file_path = os.path.join(folder_path, file_name)

        if not os.path.exists(file_path):
            print(f"Warning: File '{file_name}' does not exist at path: {file_path}")
            continue

        try:
            df = pd.read_csv(file_path)
            df = df[['Impact category', 'Unit', 'Total']].copy()
            df['Weighted Total'] = df['Total'] * (percentage / 100.0)
            mix_dfs.append(df[['Impact category', 'Unit', 'Weighted Total']])
        except Exception as e:
            print(f"Error reading {file_name}: {e}")

    if not mix_dfs:
        return pd.DataFrame(columns=['Impact category', 'Unit', 'Total'])

    # Merge and sum
    result_df = mix_dfs[0].copy()
    for df in mix_dfs[1:]:
        result_df = result_df.merge(df, on=['Impact category', 'Unit'], how='outer', suffixes=('', '_extra'))

        # Fill NaNs in case of partial matches
        for col in result_df.columns:
            if 'Weighted Total' in col and col != 'Weighted Total':
                result_df['Weighted Total'] = result_df['Weighted Total'].fillna(0) + result_df[col].fillna(0)
                result_df.drop(columns=[col], inplace=True)

    result_df.rename(columns={'Weighted Total': 'Total'}, inplace=True)
    return result_df


energy_mix = load_default_energy_mix(DEFAULT_ENERGY_MIX_FILE)
electricity_folder_path = ELECTRICITY_DIR
mixed_df = generate_energy_mix(electricity_folder_path, energy_mix)

lca_input = load_default_lca_input(DEFAULT_LCA_INPUT_FILE)

output = load_default_output(DEFAULT_OUTPUT_FILE)



import os
import pandas as pd
from collections import defaultdict

def calculate_total_lca(folder_path, lca_input, electricity_mix_df, output):
    """
    Returns normalized LCA results as a DataFrame:
    [Impact category, Unit, A1, A2, A3, A4, ...] per unit of product.

    Parameters:
    - folder_path (str): Path to CSV files
    - lca_input (dict): Process info with module, amount, unit, file_name
    - electricity_mix_df (pd.DataFrame): Impacts per 1 kWh of electricity
    - output (dict): Product output amount to normalize impact values

    Returns:
    - pd.DataFrame: Normalized environmental impacts per output unit
    """
    module_frames = defaultdict(list)

    for process_name, info in lca_input.items():
        module = info["module"]
        amount = info["Amount"]
        file_name = info["file_name"]

        if file_name.lower() == "electricity":
            df = electricity_mix_df.copy()
        else:
            file_path = os.path.join(folder_path, file_name)
            if not os.path.exists(file_path):
                print(f"⚠️ File not found: {file_path}")
                continue
            try:
                df = pd.read_csv(file_path)
            except Exception as e:
                print(f"⚠️ Error reading {file_path}: {e}")
                continue

        df = df[['Impact category', 'Unit', 'Total']].copy()
        df['Total'] = df['Total'] * amount
        df.rename(columns={'Total': module}, inplace=True)
        module_frames[module].append(df)

    # Merge all module-level data
    merged = None
    for module, dfs in module_frames.items():
        combined = pd.concat(dfs).groupby(['Impact category', 'Unit'], as_index=False).sum()
        if merged is None:
            merged = combined
        else:
            merged = pd.merge(merged, combined, on=['Impact category', 'Unit'], how='outer')

    merged.fillna(0, inplace=True)

    # Normalize by output amount
    output_amount = list(output.values())[0]['Amount']
    module_columns = [col for col in merged.columns if col not in ['Impact category', 'Unit']]

    for col in module_columns:
        merged[col] = merged[col] / output_amount

    return merged


if __name__ == "__main__":
    lca_results = calculate_total_lca(
        folder_path=DATA_DIR,
        lca_input=lca_input,
        electricity_mix_df=mixed_df,
        output=output,
    )
    print(lca_results.head())
