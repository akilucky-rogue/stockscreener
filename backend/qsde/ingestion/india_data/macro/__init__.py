"""India-native macro ingestion: RBI + MOSPI.

RBI handles rates and currency (USD/INR, policy rates, G-Sec yields).
MOSPI handles inflation and real-economy indicators (CPI, IIP, WPI, GDP).

Both persist to the `macro` table with source attribution. The macro
factor reader joins this with the equity panel for index-correlated
features (rate sensitivity, inflation hedges, etc.).
"""
