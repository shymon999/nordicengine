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

def load_config():
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"handlers": [], "vip_rules": [], "general_rules": []}

config = load_config()
HANDLERS = {h['name']: h for h in config.get('handlers', [])}
VIP_RULES = config.get('vip_rules', [])
GENERAL_RULES = sorted(config.get('general_rules', []), key=lambda x: x.get('priority', 50))

# ============================================================================
# PROCESSOR
# ============================================================================

class NordicProcessor:
    def __init__(self, active_handlers, df):
        self.active_handlers = active_handlers
        self.load_counter = Counter()
        self.today_str = datetime.now().strftime('%d.%m.%Y')
        
        # Pre-calculate eligibility for each handler across the whole file
        # This helps in prioritizing 'specialized' handlers over 'flexible' ones
        self.eligibility_count = Counter()
        for _, row in df.iterrows():
            possible_handlers = self._get_eligible_handlers_for_row(row)
            for h in possible_handlers:
                self.eligibility_count[h] += 1

    def _get_eligible_handlers_for_row(self, row):
        """Returns a list of active handlers that could potentially take this claim."""
        country = str(row.get('DSV Country (Lookup)', '')).strip()
        division = normalize_division(str(row.get('DSV Division (Lookup)', '')).strip())
        claimant = str(row.get('Claimant Name', '')).strip()
        claim_amt = safe_float(row.get('Claim amount EUR', 0))
        liability = safe_float(row.get('Total liability EUR', 0))
        eff_amt = min(claim_amt, liability) if claim_amt > 0 and liability > 0 else max(claim_amt, liability)

        # VIP Check
        for vip in VIP_RULES:
            if normalize_name_for_match(vip['customer']) in normalize_name_for_match(claimant):
                if vip.get('country') and vip['country'].lower() != country.lower(): continue
                if eff_amt >= vip.get('min_amount', 0):
                    h_name = vip['handler']
                    return [h_name] if h_name in self.active_handlers else []

        # General Rules Check
        for rule in GENERAL_RULES:
            if self._rule_matches(rule, country, division, claimant, eff_amt):
                if rule.get('output_assigned') == '#N/A': return []
                return [h for h in rule.get('handlers', []) if h in self.active_handlers]
        
        return []

    def process(self, df):
        # 1. Evaluate 'difficulty' (how many active handlers can take each claim)
        rows_with_meta = []
        for idx, row in df.iterrows():
            possible = self._get_eligible_handlers_for_row(row)
            rows_with_meta.append({'idx': idx, 'row': row, 'options': len(possible)})
        
        # 2. Sort: process rows with FEWER options first (VIPs, then restricted countries)
        rows_with_meta.sort(key=lambda x: (x['options'] == 0, x['options']))

        results_map = {}
        for item in rows_with_meta:
            handler_name, team_name, rid, reason = self._assign(item['row'])
            results_map[item['idx']] = self._build_output(item['row'], handler_name, team_name, rid, reason)
        
        # 3. Restore original order
        results = [results_map[i] for i in range(len(df))]
        output_df = pd.DataFrame(results)
        
        cols = list(output_df.columns)
        assign_cols = ['Assigned Name', 'Claim Handler', 'Team Name', 'Assignment Reason']
        for c in assign_cols:
            if c in cols: cols.remove(c)
        
        insert_idx = 0
        if 'Claimant Name' in cols:
            insert_idx = cols.index('Claimant Name') + 1
        
        output_df = output_df[cols[:insert_idx] + assign_cols + cols[insert_idx:]]
        return output_df

    def _assign(self, row):
        country = str(row.get('DSV Country (Lookup)', '')).strip()
        division = normalize_division(str(row.get('DSV Division (Lookup)', '')).strip())
        claimant = str(row.get('Claimant Name', '')).strip()
        claim_amt = safe_float(row.get('Claim amount EUR', 0))
        liability = safe_float(row.get('Total liability EUR', 0))
        eff_amt = min(claim_amt, liability) if claim_amt > 0 and liability > 0 else max(claim_amt, liability)

        # 1. VIP Rules
        for vip in VIP_RULES:
            if normalize_name_for_match(vip['customer']) in normalize_name_for_match(claimant):
                if vip.get('country') and vip['country'].lower() != country.lower(): continue
                if eff_amt >= vip.get('min_amount', 0):
                    h_name = vip['handler']
                    if h_name in self.active_handlers:
                        h = HANDLERS[h_name]
                        self.load_counter[h_name] += 1
                        return h_name, h['team'], h['riskonnect_id'], f"VIP: {vip['customer']}"
                    break

        # 2. General Rules
        for rule in GENERAL_RULES:
            if not self._rule_matches(rule, country, division, claimant, eff_amt): continue
            if rule.get('output_assigned') == '#N/A':
                return None, rule.get('output_team', 'Nordic'), '#N/A', f"Rule: {rule['description']}"

            possible_handlers = [h for h in rule.get('handlers', []) if h in self.active_handlers]
            if not possible_handlers: continue

            # SMART PICK:
            # 1. Pick handler with minimum current load (load_counter)
            # 2. Tie-breaker: Pick handler who has FEWER total options in this file (less flexible)
            #    This saves flexible handlers for claims ONLY they can handle.
            selected = min(possible_handlers, 
                           key=lambda x: (self.load_counter[x], self.eligibility_count[x]))
            
            self.load_counter[selected] += 1
            h = HANDLERS[selected]
            return selected, rule.get('output_team', h['team']), h['riskonnect_id'], f"Rule: {rule['description']}"

        return None, 'CHC Nordic', '#N/A', "No matching rule"

    def _rule_matches(self, rule, country, division, claimant, eff_amt):
        if rule.get('countries') and country not in rule['countries']: return False
        if rule.get('divisions') and division not in rule['divisions']: return False
        if rule.get('customer_contains'):
            if not any(c.lower() in claimant.lower() for c in rule['customer_contains']): return False
        if rule.get('min_amount') is not None and eff_amt < rule['min_amount']: return False
        if rule.get('max_amount') is not None and eff_amt >= rule['max_amount']: return False
        return True

    def _build_output(self, row, handler_name, team_name, rid, reason):
        r = row.copy().astype(object)
        if 'Claim: Claim Number' in r.index:
            r = r.rename({'Claim: Claim Number': 'Claim Import ID'})
        
        dol = row.get('Date of Loss')
        if pd.notna(dol):
            try:
                if isinstance(dol, str): dol = pd.to_datetime(dol, dayfirst=True)
                r['Date of Loss'] = dol.strftime('%d.%m.%Y')
                timebar = dol + timedelta(days=365)
                r['Timebar date liable party'] = timebar.strftime('%d.%m.%Y')
            except: pass

        r['Assigned Name'] = rid or ''
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
        st.info("Zaznacz osoby z zespołu Nordic.")
        
        active_handlers = []
        teams = {}
        for h in HANDLERS.values():
            teams.setdefault(h['team'], []).append(h['name'])
        
        for t_name, h_names in teams.items():
            if t_name == "Nordic":
                st.subheader(t_name)
                for h_name in sorted(h_names):
                    if st.checkbox(h_name, value=True, key=f"att_{h_name}"):
                        active_handlers.append(h_name)
            else:
                active_handlers.extend(h_names)

    if not active_handlers:
        st.warning("⚠️ Proszę zaznaczyć przynajmniej jedną osobę na liście obecności.")
        return

    uploaded = st.file_uploader("Wgraj plik Excel (.xlsx)", type=['xlsx'])
    
    if uploaded:
        df = pd.read_excel(uploaded, engine='openpyxl')
        st.success(f"Wczytano **{len(df)}** reklamacji.")
        
        if st.button("🚀 ROZPOCZNIJ PRZYDZIAŁ", type="primary", use_container_width=True):
            processor = NordicProcessor(active_handlers, df)
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
        assigned_count = len(result_df[result_df['Claim Handler'] != ''])
        col1.metric("Wszystkie", len(result_df))
        col2.metric("Przypisane", assigned_count)
        col3.metric("Brak dopasowania", len(result_df) - assigned_count)

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
