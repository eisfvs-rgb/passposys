"""
constants.py
------------
Static reference data used across the app: nationality/country code
mappings and marital-status options for form dropdowns, plus a small
sorting helper for the "Nusuk" group view (adults before children,
oldest to youngest).

Pulled out of app.py since none of this depends on Flask, the database,
or OCR — it's pure lookup data.
"""

from datetime import datetime
from time_utils import ist_now

# =====================================================
# NATIONALITY MAPPING
# =====================================================

NATIONALITY_CODE_MAP = {
    'AFG': 2, 'ALB': 5, 'DZA': 59, 'ASM': 10, 'AND': 6, 'AGO': 3, 'AIA': 4,
    'ATA': 11, 'ATG': 13, 'ARG': 8, 'ARM': 9, 'ABW': 1, 'AUS': 15, 'AUT': 14,
    'AZE': 16, 'BHS': 24, 'BHR': 23, 'BGD': 21, 'BRB': 31, 'BLR': 26, 'BEL': 18,
    'BLZ': 27, 'BEN': 19, 'BMU': 28, 'BTN': 33, 'BOL': 29, 'BIH': 25, 'BWA': 231,
    'BVT': 34, 'BRA': 30, 'IOT': 241, 'BRN': 32, 'BGR': 22, 'BFA': 20, 'BDI': 17,
    'KHM': 106, 'CMR': 42, 'CAN': 36, 'CPV': 47, 'CYM': 51, 'CAF': 35, 'TCD': 194,
    'CHL': 39, 'CHN': 40, 'CXR': 50, 'CCK': 37, 'COL': 45, 'COM': 46, 'COG': 43,
    'COK': 44, 'CRI': 48, 'CIV': 41, 'HRV': 90, 'CUB': 49, 'CYP': 52, 'CZE': 53,
    'DNK': 57, 'DJI': 55, 'DMA': 56, 'DOM': 58, 'TLS': 228, 'ECU': 60, 'EGY': 61,
    'SLV': 181, 'GNQ': 79, 'ERI': 224, 'EST': 63, 'ETH': 64, 'FLK': 67, 'FRO': 69,
    'FJI': 66, 'FIN': 65, 'FRA': 68, 'GUF': 84, 'PYF': 167, 'GAB': 70, 'GMB': 77,
    'GEO': 72, 'DEU': 54, 'GHA': 73, 'GIB': 74, 'GRC': 80, 'GRL': 82, 'GRD': 81,
    'GLP': 76, 'GUM': 85, 'GTM': 83, 'GIN': 75, 'GNB': 78, 'GUY': 86, 'HTI': 91,
    'HMD': 88, 'HND': 89, 'HKG': 1278, 'HUN': 92, 'ISL': 98, 'IND': 94, 'IDN': 93,
    'IRN': 96, 'IRQ': 97, 'IRL': 95, 'ITA': 99, 'JAM': 100, 'JPN': 102, 'JOR': 101,
    'KAZ': 103, 'KEN': 104, 'KIR': 107, 'KOR': 109, 'PRK': 164, 'XKX': 87, 'KWT': 110,
    'KGZ': 105, 'LAO': 111, 'LVA': 121, 'LBN': 112, 'LSO': 118, 'LBR': 113, 'LBY': 114,
    'LIE': 116, 'LTU': 119, 'LUX': 120, 'MAC': 122, 'MKD': 236, 'MDG': 126, 'MWI': 140,
    'MYS': 141, 'MDV': 127, 'MLI': 130, 'MLT': 131, 'MHL': 129, 'MTQ': 138, 'MRT': 135,
    'MUS': 139, 'MYT': 142, 'MEX': 128, 'FSM': 229, 'MCO': 124, 'MNG': 133, 'MNE': 243,
    'MSR': 137, 'MAR': 123, 'MOZ': 134, 'MMR': 132, 'NAM': 143, 'NRU': 153, 'NPL': 152,
    'NLD': 150, 'NCL': 144, 'NZL': 154, 'NIC': 148, 'NER': 145, 'NGA': 147, 'NIU': 149,
    'NFK': 146, 'MNP': 227, 'NOR': 151, 'OMN': 155, 'PAK': 156, 'PLW': 235, 'PSE': 234,
    'PAN': 157, 'PNG': 161, 'PRY': 166, 'PER': 159, 'PHL': 160, 'PCN': 158, 'POL': 162,
    'PRT': 165, 'PRI': 163, 'QAT': 168, 'MDA': 125, 'SSD': 136, 'REU': 169, 'ROU': 170,
    'RUS': 171, 'RWA': 172, 'SHN': 233, 'KNA': 108, 'LCA': 115, 'SPM': 184, 'VCT': 212,
    'WSM': 226, 'SMR': 182, 'STP': 185, 'SEN': 175, 'SRB': 242, 'SYC': 191, 'SLE': 180,
    'SGP': 176, 'SVK': 187, 'SVN': 188, 'SLB': 179, 'SOM': 183, 'ZAF': 219, 'SGS': 177,
    'ESP': 62, 'LKA': 117, 'SDN': 174, 'SUR': 186, 'SJM': 178, 'SWZ': 190, 'SWE': 189,
    'CHE': 38, 'SYR': 192, 'TWN': 205, 'TJK': 197, 'THA': 196, 'TGO': 195, 'TKL': 198,
    'TON': 200, 'TTO': 201, 'TUN': 202, 'TUR': 203, 'TKM': 199, 'TCA': 193, 'TUV': 204,
    'UGA': 207, 'UKR': 208, 'ARE': 7, 'GBR': 71, 'USA': 210, 'TZA': 206, 'URY': 209,
    'UZB': 211, 'VUT': 216, 'VAT': 240, 'VEN': 213, 'VNM': 215, 'VGB': 214, 'VIR': 232,
    'WLF': 217, 'YEM': 218, 'ZMB': 221, 'ZWE': 222
}

# Reverse lookup: nationality_id -> MRZ country code (used to keep
# passports.country in sync when the nationality is edited inline)
NATIONALITY_ID_TO_COUNTRY_CODE = {v: k for k, v in NATIONALITY_CODE_MAP.items()}

NATIONALITY_OPTIONS = [
    (2, "Afghanistan"), (5, "Albania"), (59, "Algeria"), (10, "American Samoa"),
    (6, "Andorra"), (3, "Angola"), (4, "Anguilla"), (11, "Antarctic"),
    (13, "Antigua"), (8, "Argentina"), (9, "Armenia"), (1, "Aruba"),
    (15, "Australia"), (14, "Austria"), (16, "Azerbaijan"), (24, "Bahamas"),
    (23, "Bahrain"), (21, "Bangladesh"), (31, "Barbados"), (26, "Belarus"),
    (18, "Belgium"), (27, "Belize"), (19, "Benin"), (28, "Bermuda"),
    (33, "Bhutan"), (29, "Bolivia"), (25, "Bosnia"), (231, "Botswana"),
    (34, "Bouvet Island"), (30, "Brazil"), (241, "British Indian Ocean Territory"),
    (32, "Brunei"), (22, "Bulgaria"), (20, "Burkina Faso"), (17, "Burundi"),
    (106, "Cambodia"), (42, "Cameroon"), (36, "Canada"), (47, "Cape Verde"),
    (51, "Cayman Island"), (35, "Central African Republic"), (194, "Chad"),
    (39, "Chile"), (40, "China"), (50, "Christmas Island"), (37, "Cocos Island"),
    (45, "Colombia"), (46, "Comoros"), (43, "Congo"), (44, "Cook Island"),
    (48, "Costa Rica"), (41, "Cote Divoire"), (90, "Croatia"), (49, "Cuba"),
    (52, "Cyprus"), (53, "Czech Republic"), (57, "Denmark"), (55, "Djibouti"),
    (56, "Dominica"), (58, "Dominican Republic"), (228, "East Timor"), (60, "Ecuador"),
    (61, "Egypt"), (181, "El Salvador"), (79, "Equatorial Guinea"), (224, "Eritrea"),
    (63, "Estonia"), (64, "Ethiopia"), (67, "Falkland Islands"), (69, "Faroe Islands"),
    (66, "Fiji"), (65, "Finland"), (68, "France"), (237, "France, Metropolitan"),
    (84, "French Guiana"), (167, "French Polynesia"), (12, "French Southern and Antarctic"),
    (70, "Gabon"), (77, "Gambia"), (72, "Georgia"), (54, "Germany"), (73, "Ghana"),
    (74, "Gibraltar"), (80, "Greece"), (82, "Greenland"), (81, "Grenada"),
    (76, "Guadeloupe"), (85, "Guam"), (83, "Guatemala"), (75, "Guinea"),
    (78, "Guinea-Bissau"), (86, "Guyana"), (91, "Haiti"), (88, "Heard Island and McDonald Island"),
    (89, "Honduras"), (1278, "Hong Kong China"), (92, "Hungary"), (98, "Iceland"),
    (94, "India"), (93, "Indonesia"), (96, "Iran"), (97, "Iraq"), (95, "Ireland"),
    (99, "Italy"), (100, "Jamaica"), (102, "Japan"), (101, "Jordan"), (103, "Kazakhstan"),
    (104, "Kenya"), (107, "Kiribati"), (109, "Korea , South"), (164, "Korea, North"),
    (87, "Kosovo"), (110, "Kuwait"), (105, "Kyrgyzstan"), (111, "Laos"), (121, "Latvia"),
    (112, "Lebanon"), (118, "Lesotho"), (113, "Liberia"), (114, "Libya"), (116, "Liechtenstein"),
    (119, "Lithuania"), (120, "Luxembourg"), (122, "Macau China"), (236, "Macedonia, The Former Yugoslav Republic of"),
    (126, "Madagascar"), (140, "Malawi"), (141, "Malaysia"), (127, "Maldives"), (130, "Mali"),
    (131, "Malta"), (129, "Marshall Island"), (230, "Marshall Islands"), (138, "Martinique"),
    (135, "Mauritania"), (139, "Mauritius"), (142, "Mayotte"), (128, "Mexico"),
    (229, "Micronesia , Federated Stat"), (124, "Monaco"), (133, "Mongolia"), (243, "Montenegro"),
    (137, "Montserrat"), (123, "Morocco"), (134, "Mozambique"), (132, "Myanmar"), (143, "Namibia"),
    (153, "Nauru"), (152, "Nepal"), (150, "Netherlands"), (238, "Netherlands Antilles"),
    (144, "New Caledonia"), (154, "New Zealand"), (148, "Nicaragua"), (145, "Niger"),
    (147, "Nigeria"), (149, "Niue"), (146, "Norfolk Island"), (227, "Northern Mariana Islands"),
    (151, "Norway"), (155, "Oman"), (156, "Pakistan"), (235, "Palau"), (234, "Palestinian Territory, Occupied"),
    (157, "Panama"), (161, "Papua New Guinea"), (166, "Paraguay"), (159, "Peru"), (160, "Philippines"),
    (158, "Pitcairn Islands"), (162, "Poland"), (165, "Portugal"), (163, "Puerto Rico"), (168, "Qatar"),
    (125, "Republic of Moldova"), (136, "Republic of South Sudan"), (169, "Reunion"), (170, "Romania"),
    (171, "Russian Federation"), (172, "Rwanda"), (233, "Saint Helena"), (108, "Saint Kitts and Nevis"),
    (115, "Saint Lucia"), (184, "Saint Pierre and Miquelon"), (212, "Saint Vincent and the Grenadines"),
    (226, "Samoa"), (182, "San Marino"), (185, "Sao Tome And Principe"), (175, "Senegal"),
    (242, "Serbia"), (223, "Serbia and Montenegro"), (191, "Seychelles"), (180, "Sierra Leone"),
    (176, "Singapore"), (187, "Slovak Republic"), (188, "Slovenia"), (179, "Solomon Islands"),
    (183, "Somalia"), (219, "South Africa"), (177, "South Georgia and The South"), (62, "Spain"),
    (117, "Sri Lanka"), (174, "Sudan"), (186, "Suriname"), (178, "Svalbard"), (190, "Swaziland"),
    (189, "Sweden"), (38, "Switzerland"), (192, "Syrian"), (205, "Taiwan China"), (197, "Tajikistan"),
    (196, "Thailand"), (195, "Togo"), (198, "Tokelau"), (200, "Tonga"), (201, "Trinidad and Tobago"),
    (202, "Tunisia"), (203, "Turkey"), (199, "Turkmenistan"), (193, "Turks and Caicos Islands"),
    (204, "Tuvalu"), (207, "Uganda"), (208, "Ukraine"), (7, "United Arab Emirates"), (71, "United Kingdom"),
    (210, "United States"), (239, "United States Minor Outlying Islands"), (206, "UR Tanzania"),
    (209, "Uruguay"), (211, "Uzbekistan"), (216, "Vanuatu"), (240, "Vatican City State"),
    (213, "Venezuela"), (215, "Vietnam"), (214, "Virgin Islands"), (232, "Virgin Islands(U.S.)"),
    (217, "Wallis and Futuna"), (218, "Yemen"), (225, "Yugoslavia"), (220, "Zaire"), (221, "Zambia"),
    (222, "Zimbabwe")
]

MARITAL_STATUS_OPTIONS = [
    (1, "Single"), (2, "Married"), (3, "Divorced"), (4, "Widow"), (5, "Other")
]

def sort_nusuk_group(rows):
    today = ist_now().date()
    
    def get_sorting_key(row):
        dob = row.get('dob')
        if not dob:
            # Move records with missing/invalid DOBs to the very end
            return (2, datetime.max.date())
        
        # Calculate approximate age in years
        age = (today - dob).days / 365.25
        
        # Priority 0 = Adult (12+), Priority 1 = Child (<12)
        priority = 0 if age >= 12 else 1
        
        # Sort by group priority first, then oldest-to-youngest within the group
        return (priority, dob)

    # Sort the database rows in-place
    rows.sort(key=get_sorting_key)
    return rows
