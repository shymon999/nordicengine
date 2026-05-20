import streamlit as st
import pandas as pd
import json
import re
import io
from datetime import datetime, timedelta
from collections import Counter

st.set_page_config(page_title="Nordic Claim Engine", page_icon="🎯", layout="wide")

# ============================================================================
# UTILS
# ============================================================================

def normalize_division(div):
    if not div: return ''
    div = str(div).strip()
    low = div.lower().replace(' ', '')
    if 'air' in div.lower() and 'sea' in div.lower(): return 'A&S'
    if low in ('a&s', 'as', 'airsea', 'air&sea'): return 'A&S'
    if 'xpress' in div.lower(): return 'XPress'
    if 'solution' in div.lower(): return 'Solutions'
    if 'road' in div.lower(): return 'Road'
    if 'contract' in div.lower() and 'logistic' in div.lower(): return 'Contract Logistics'
    return div

def normalize_name_for_match(name):
    if not name: return ""
    replacements = {
        'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n',
        'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
        'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N',
        'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z',
    }
    result = str(name)
    for pl, ascii_char in replacements.items():
        result = result.replace(pl, ascii_char)
    result = re.sub(r'[\s\-\.\,\_]', '', result)
    return result.lower().strip()

def safe_float(v):
    try:
        if pd.isna(v): return 0.0
        return float(v)
    except: return 0.0

# ============================================================================
# DATA LOADING
# ============================================================================

@st.cache_data
def load_config():
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()
HANDLERS = {h['name']: h for h in config['handlers']}
VIP_RULES = config['vip_rules']
GENERAL_RULES = sorted(config['general_rules'], key=lambda x: x['priority'])

# ============================================================================
# PROCESSOR
# ============================================================================

class NordicProcessor:
    def __init__(self, active_handlers):
        self.active_handlers = active_handlers
        self.load_counter = Counter()
        self.today_str = datetime.now().strftime('%d.%m.%Y')

    def process(self, df):
        results = []
        for _, row in df.iterrows():
            handler_name, team_name, rid, reason = self._assign(row)
            results.append(self._build_output(row, handler_name, team_name, rid, reason))
        
        output_df = pd.DataFrame(results)
        
        # Reorder columns to have assignment info prominently
        cols = list(output_df.columns)
        for c in ['Assigned Name', 'Claim Handler', 'Team Name', 'Assignment Reason']:
            if c in cols: cols.remove(c)
        
        insert_idx = 0
        if 'Claimant Name' in cols:
            insert_idx = cols.index('Claimant Name') + 1
        
        output_df = output_df[cols[:insert_idx] + ['Assigned Name', 'Claim Handler', 'Team Name', 'Assignment Reason'] + cols[insert_idx:]]
        return output_df

    def _assign(self, row):
        country = str(row.get('DSV Country (Lookup)', '')).strip()
        division = normalize_division(str(row.get('DSV Division (Lookup)', '')).strip())
        claimant = str(row.get('Claimant Name', '')).strip()
        sub_type = str(row.get('Claim Sub-Type', '')).strip()
        
        claim_amt = safe_float(row.get('Claim amount EUR', 0))
        liability = safe_float(row.get('Total liability EUR', 0))
        eff_amt = min(claim_amt, liability) if claim_amt > 0 and liability > 0 else max(claim_amt, liability)

        # 1. VIP Rules
        for vip in VIP_RULES:
            if normalize_name_for_match(vip['customer']) in normalize_name_for_match(claimant):
                if vip['country'] and vip['country'].lower() != country.lower():
                    continue
                if eff_amt >= vip['min_amount']:
                    h_name = vip['handler']
                    if h_name in self.active_handlers:
                        h = HANDLERS[h_name]
                        self.load_counter[h_name] += 1
                        return h_name, h['team'], h['riskonnect_id'], f"VIP: {vip['customer']}"
                    else:
                        # VIP handler not present - fallback to general rules? 
                        # Or maybe assign anyway but mark as absent? 
                        # Let's fallback to general rules for now.
                        break

        # 2. General Rules
        for rule in GENERAL_RULES:
            if not self._rule_matches(rule, country, division, sub_type, claimant, eff_amt):
                continue
            
            # Static override (e.g. LEGO -> Global #N/A)
            if rule.get('output_assigned') == '#N/A':
                return None, rule.get('output_team', 'Nordic'), '#N/A', f"Rule: {rule['description']}"

            # Pick handler from rule
            possible_handlers = [h for h in rule.get('handlers', []) if h in self.active_handlers]
            if not possible_handlers:
                continue # Try next rule if no active handlers for this one

            # Equal distribution pick
            selected = min(possible_handlers, key=lambda x: self.load_counter[x])
            self.load_counter[selected] += 1
            h = HANDLERS[selected]
            return selected, rule.get('output_team', h['team']), h['riskonnect_id'], f"Rule: {rule['description']}"

        return None, 'CHC Nordic', '#N/A', "No matching rule"

    def _rule_matches(self, rule, country, division, sub_type, claimant, eff_amt):
        if rule.get('countries') and country not in rule['countries']: return False
        if rule.get('divisions') and division not in rule['divisions']: return False
        if rule.get('sub_types'):
            if not any(s.lower() in sub_type.lower() for s in rule['sub_types']): return False
        if rule.get('customer_contains'):
            if not any(c.lower() in claimant.lower() for c in rule['customer_contains']): return False
        if rule.get('min_amount') is not None and eff_amt < rule['min_amount']: return False
        if rule.get('max_amount') is not None and eff_amt >= rule['max_amount']: return False
        return True

    def _build_output(self, row, handler_name, team_name, rid, reason):
        r = row.copy().astype(object)
        if 'Claim: Claim Number' in r.index:
            r = r.rename({'Claim: Claim Number': 'Claim Import ID'})
        
        # Date logic
        dol = row.get('Date of Loss')
        if pd.notna(dol):
            try:
                if isinstance(dol, str): dol = pd.to_datetime(dol, dayfirst=True)
                r['Date of Loss'] = dol.strftime('%d.%m.%Y')
                timebar = dol + timedelta(days=365)
                r['Timebar date liable party'] = timebar.strftime('%d.%m.%Y')
            except: pass

        r['Assigned Name'] = rid or '#N/A'
        r['Claim Handler'] = handler_name or ''
        r['Team Name'] = team_name
        r['Assignment Reason'] = reason
        r['Internal Status'] = 'Awaiting own process'
        r['Recovery Status'] = 'Awaiting own process'
        r['Initial assignment'] = self.today_str
        if str(row.get('Status', '')).strip().lower() == 'new':
            r['Status'] = 'Assigned'
        return r

# ============================================================================
# UI
# ============================================================================

def main():
    st.title("🎯 Nordic Claim Engine")
    st.caption("Simplified version — Nordic only, local config, no cloud DB.")

    with st.sidebar:
        st.header("👥 Lista Obecności")
        st.info("Zaznacz osoby, które biorą udział w dzisiejszym przydziale.")
        
        active_handlers = []
        # Group by team
        teams = {}
        for h in HANDLERS.values():
            teams.setdefault(h['team'], []).append(h['name'])
        
        for t_name, h_names in teams.items():
            st.subheader(t_name)
            for h_name in sorted(h_names):
                if st.checkbox(h_name, value=True, key=f"att_{h_name}"):
                    active_handlers.append(h_name)

    if not active_handlers:
        st.warning("⚠️ Proszę zaznaczyć przynajmniej jedną osobę na liście obecności.")
        return

    uploaded = st.file_uploader("Wgraj plik Excel (.xlsx)", type=['xlsx'])
    
    if uploaded:
        df = pd.read_excel(uploaded, engine='openpyxl')
        st.success(f"Wczytano **{len(df)}** reklamacji.")
        
        if st.button("🚀 ROZPOCZNIJ PRZYDZIAŁ", type="primary", use_container_width=True):
            processor = NordicProcessor(active_handlers)
            with st.spinner("Przetwarzanie..."):
                result_df = processor.process(df)
                st.session_state['result_df'] = result_df
                st.session_state['stats'] = processor.load_counter

    if 'result_df' in st.session_state:
        result_df = st.session_state['result_df']
        stats = st.session_state['stats']
        
        st.divider()
        st.header("📊 Wyniki")
        
        col1, col2, col3 = st.columns(3)
        assigned = len(result_df[result_df['Assigned Name'] != '#N/A'])
        col1.metric("Wszystkie", len(result_df))
        col2.metric("Przypisane", assigned)
        col3.metric("Brak dopasowania", len(result_df) - assigned)

        st.subheader("Obciążenie handlerów")
        if stats:
            stats_data = [{"Handler": name, "Liczba spraw": count, "Team": HANDLERS[name]['team']} 
                          for name, count in stats.items()]
            st.dataframe(pd.DataFrame(stats_data).sort_values("Liczba spraw", ascending=False), 
                         use_container_width=True, hide_index=True)

        st.subheader("Podgląd przydziału")
        st.dataframe(result_df, use_container_width=True)

        st.subheader("Pobierz plik")
        xlsx_buf = io.BytesIO()
        result_df.to_excel(xlsx_buf, index=False, engine='openpyxl')
        filename = f"Rozdanie_Nordic_{datetime.now().strftime('%d-%m-%Y')}.xlsx"
        st.download_button("📥 Pobierz Excel", xlsx_buf.getvalue(), filename, 
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)

if __name__ == "__main__":
    main()
