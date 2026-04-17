# Simple LCA Tool

A Flask-based web application for calculating and exploring Life Cycle Assessment (LCA) results for a wood product system.

The app lets you:

- adjust the electricity energy mix,
- update default LCA process inputs,
- recalculate impacts by module,
- compare multiple scenarios,
- run a simple sensitivity analysis,
- use an optional OpenAI-powered chatbot to interpret or modify analysis inputs.

## Features

- Interactive web UI built with Flask, Bootstrap, and Chart.js
- CSV-driven default configuration for energy mix and LCA inputs
- Module-level LCA result calculation
- Scenario comparison across custom energy mixes and process inputs
- Sensitivity analysis for selected impact categories
- Optional basic authentication
- Optional OpenAI integration for chatbot-driven updates and result explanations

## Project Structure

```text
.
|-- app.py
|-- Functions.py
|-- app.spec
|-- README.md
|-- .env.example
|-- .gitignore
|-- data/
|   |-- default_energy_mix.csv
|   |-- default_lca_input.csv
|   |-- Electricity/
|   `-- other LCA input CSV files
`-- templates/
    `-- index.html
```

## Configuration

All secrets and deployment-specific settings should come from environment variables.

Use `.env.example` as your template.

### Required for optional features

- `OPENAI_API_KEY`
  Required only if you want the chatbot and AI-generated explanations.

### Recommended

- `FLASK_SECRET_KEY`
  Set this in any real deployment.

### Optional

- `PORT`
  Defaults to `5000`.
- `FLASK_DEBUG`
  `true` or `false`.
- `SESSION_COOKIE_SECURE`
  Controls whether session cookies require HTTPS.
- `BASIC_AUTH_ENABLED`
  Set to `true` to protect the app with HTTP Basic Auth.
- `BASIC_AUTH_USERNAME`
  Required if `BASIC_AUTH_ENABLED=true`.
- `BASIC_AUTH_PASSWORD`
  Required if `BASIC_AUTH_ENABLED=true`.

## Default Input Files

The app no longer stores default energy mix values or default LCA input values directly in the code.

Instead, it reads them from:

- [data/default_energy_mix.csv](data/default_energy_mix.csv)
- [data/default_lca_input.csv](data/default_lca_input.csv)
- [data/default_output.csv](data/default_output.csv)

### `default_energy_mix.csv`

Expected columns:

- `source`
- `percentage`

Example:

```csv
source,percentage
biomass,0.42562
coal,31.39936
solar,0.50886
```

### `default_lca_input.csv`

Expected columns:

- `process`
- `module`
- `amount`
- `unit`

Example:

```csv
process,module,amount,unit
A3 - Pressing - Electricity,A3,982800,kWh
A3 - Pressing - Natural Gas,A3,5317481.49,MJ
```

The app infers the matching source file automatically from the process name. Electricity processes are routed to the generated electricity mix instead of a standalone CSV.

### `default_output.csv`

Expected columns:

- `name`
- `amount`
- `unit`

Example:

```csv
name,amount,unit
InventWood_Output,516897,kg
```

## Installation

Install the Python dependencies in your environment:

```bash
pip install flask pandas openai
```

If you use a virtual environment, activate it first.

## Running the App

1. Copy `.env.example` to `.env` and fill in the values you need.
2. Export the environment variables or load them through your preferred workflow.
3. Start the app:

```bash
python app.py
```

By default, the app runs on:

- `http://127.0.0.1:5000`

## Main Endpoints

- `GET /`
  Main application UI
- `POST /update_energy_mix`
  Update the current energy mix in session
- `POST /update_lca_input`
  Update current LCA inputs in session and recalculate results
- `POST /compare_scenarios`
  Compare multiple scenarios
- `POST /sensitivity_analysis`
  Run sensitivity analysis on current inputs
- `POST /chatbot`
  Optional AI assistant endpoint

## Notes

- `Functions.py` contains a standalone helper script version of the same calculation logic.
- `app.spec` is included for PyInstaller packaging.
- The frontend template is in [templates/index.html](templates/index.html).
- The app stores working values in the Flask session during use.

## Security Notes

- Do not commit `.env` or any secret-bearing files.
- Rotate any API keys that were previously embedded in local code before publishing the repository.
- If you expose the app publicly, set `FLASK_SECRET_KEY`, enable HTTPS, and review whether `BASIC_AUTH_ENABLED` should be turned on.
