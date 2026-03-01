#!/usr/bin/env python3
"""
HTML Generators — Dark Cinematic Luxury Edition
=================================================
Two generators: executive email + interactive web experience.
Paste this block into newsletter_pipeline.py between process_articles() and send_email().
"""

# ---------------------------------------------------------------------------
# Inline SVG Icons (email-safe data URIs, dark luxury palette)
# ---------------------------------------------------------------------------

SVG_ICON_MARKET = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23261A00"/%3E%3Cpath d="M8 23l5-6 5 4 7-10" stroke="%23F59E0B" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/%3E%3Ccircle cx="25" cy="11" r="2.5" fill="%23FBBF24"/%3E%3C/svg%3E'
SVG_ICON_RESEARCH = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23052E16"/%3E%3Ccircle cx="14" cy="14" r="6" stroke="%2334D399" stroke-width="2.5"/%3E%3Cpath d="M19 19l5 5" stroke="%2310B981" stroke-width="2.5" stroke-linecap="round"/%3E%3Ccircle cx="14" cy="14" r="2" fill="%2334D399" opacity="0.5"/%3E%3C/svg%3E'
SVG_ICON_TOOL = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23172554"/%3E%3Crect x="8" y="5" width="16" height="22" rx="3" stroke="%2360A5FA" stroke-width="2"/%3E%3Cpath d="M12 11h8M12 15h8M12 19h4" stroke="%2393C5FD" stroke-width="1.5" stroke-linecap="round"/%3E%3C/svg%3E'
SVG_ICON_RISK = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%23450A0A"/%3E%3Cpath d="M16 6l10 18H6L16 6z" stroke="%23F87171" stroke-width="2" stroke-linejoin="round"/%3E%3Cpath d="M16 14v4" stroke="%23FCA5A5" stroke-width="2" stroke-linecap="round"/%3E%3Ccircle cx="16" cy="21" r="1.2" fill="%23FCA5A5"/%3E%3C/svg%3E'
SVG_ICON_OPP = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="32" height="32" fill="none"%3E%3Crect width="32" height="32" rx="8" fill="%232E1065"/%3E%3Ccircle cx="16" cy="16" r="5" stroke="%23A78BFA" stroke-width="2"/%3E%3Cpath d="M16 6v4M16 22v4M6 16h4M22 16h4" stroke="%23C4B5FD" stroke-width="2" stroke-linecap="round"/%3E%3C/svg%3E'
SVG_ICON_LOGO = 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="44" height="44" fill="none"%3E%3Crect width="44" height="44" rx="12" fill="%230C1222"/%3E%3Cdefs%3E%3ClinearGradient id="lg" x1="0" y1="0" x2="44" y2="44"%3E%3Cstop stop-color="%233B82F6"/%3E%3Cstop offset="1" stop-color="%238B5CF6"/%3E%3C/linearGradient%3E%3C/defs%3E%3Cpath d="M22 8c-5 0-9 3.5-9 8 0 2.5 1.2 4.5 3 6-1 1.5-2 4-2 6.5 0 4.5 3.5 8 8 8s8-3.5 8-8c0-2.5-1-5-2-6.5 1.8-1.5 3-3.5 3-6 0-4.5-4-8-9-8z" stroke="url(%23lg)" stroke-width="1.8" fill="none"/%3E%3Ccircle cx="22" cy="18" r="3" fill="%233B82F6" opacity="0.3"/%3E%3Cpath d="M18 18h8M19 14h6M19 22h5" stroke="url(%23lg)" stroke-width="1" stroke-linecap="round" opacity="0.5"/%3E%3C/svg%3E'
