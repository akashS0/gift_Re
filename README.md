# Gift Re — Quant Fund (Model A)

Code for the BiLSTM stock-selection model behind Gift Re's investment engine, the pipeline that turns a
quarter's raw fundamentals into model predictions, and the research portal that presents it.

## What's here

| Folder | What it does |
|---|---|
| `Quant Fund 4/Code/` | Notebooks: data processing, feature selection, and training of the BiLSTM, CNN, LSTM, TFT and logistic models |
| `frontend/` | The research portal (React, `src/App.jsx`, built to `app.js`), plus the standalone Working model, Check outputs and Factor builder pages |
| `api/` | Local web service (FastAPI) behind the Check outputs and Factor builder pages: builds the 11 factors from a raw export and scores them |
| `database/` | Builds and loads the Supabase price database and the model output table |
| `pipeline.py`, `app.py` | Out-of-time prediction pipeline and a simple Streamlit front end |
| `oot_preds*.ipynb`, `Data Preprocess.ipynb` | Prediction and preprocessing notebooks |

## Not included

Data, trained models and credentials are kept out of this repository: price files and databases, input and
output spreadsheets, `.keras` / `.pkl` / `.npy` model files, and `.env`. To run the code you need them locally:

- `.env` in the project root with `SUPABASE_DB_URL=...` (see `database/README.md`)
- trained model files in `model files/` (see `api/scoring.py` for the names it looks for)
- `OPENFIGI_API_KEY` in your environment for `isin_mapping.ipynb`

## Running

```
pip install -r requirements.txt
python api/main.py                                   # service on http://127.0.0.1:8100
python -m http.server 8000 --directory frontend      # portal on http://localhost:8000
```

Rebuilding the portal after editing `frontend/src/App.jsx`: `cd frontend && npm install && npm run build`.
