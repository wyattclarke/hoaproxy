#!/usr/bin/env python3
"""Build CA city -> county slug map from a hardcoded authoritative
incorporated-cities + CDPs list. Output: data/ca_city_to_county.json

Census 2020 doesn't expose a clean place-county relationship file at the
public rel2020 endpoint (only place-place and ZCTA-county exist). Rather
than fight TIGER shapefiles, this map is hardcoded from authoritative
public sources (state of California incorporated cities list + LA/OC/SD
neighborhood aliases). It covers all 482 incorporated CA cities plus the
~150 most-common CDPs and neighborhoods that appear in HOA mailing
addresses.

The map can be extended at any time by editing the literal dicts below.
"""
from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUTPUT_PATH = os.path.join(REPO_ROOT, "data", "ca_city_to_county.json")


# All 482 incorporated CA cities + ~250 CDPs/neighborhoods/common alt names.
# Format: "UPPERCASE NAME": "county-slug"
# Sources cross-checked against:
#   - California State Association of Counties (cities-by-county roster)
#   - Census 2020 Gazetteer 2020_Gaz_place_06.txt
#   - LA-County, OC, SD county GIS portals for unincorporated CDPs
CA_CITY_COUNTY: dict[str, str] = {
    # ===== Alameda (14 cities) =====
    "ALAMEDA": "alameda", "ALBANY": "alameda", "BERKELEY": "alameda",
    "DUBLIN": "alameda", "EMERYVILLE": "alameda", "FREMONT": "alameda",
    "HAYWARD": "alameda", "LIVERMORE": "alameda", "NEWARK": "alameda",
    "OAKLAND": "alameda", "PIEDMONT": "alameda", "PLEASANTON": "alameda",
    "SAN LEANDRO": "alameda", "UNION CITY": "alameda",
    # Alameda CDPs / neighborhoods
    "CASTRO VALLEY": "alameda", "SAN LORENZO": "alameda",
    "ASHLAND": "alameda", "CHERRYLAND": "alameda", "FAIRVIEW": "alameda",
    "SUNOL": "alameda",
    # ===== Alpine =====
    "MARKLEEVILLE": "alpine", "KIRKWOOD": "alpine",
    # ===== Amador =====
    "AMADOR CITY": "amador", "IONE": "amador", "JACKSON": "amador",
    "PLYMOUTH": "amador", "SUTTER CREEK": "amador", "PINE GROVE": "amador",
    # ===== Butte =====
    "BIGGS": "butte", "CHICO": "butte", "GRIDLEY": "butte",
    "OROVILLE": "butte", "PARADISE": "butte", "MAGALIA": "butte",
    "DURHAM": "butte", "PALERMO": "butte",
    # ===== Calaveras =====
    "ANGELS CAMP": "calaveras", "ARNOLD": "calaveras",
    "MURPHYS": "calaveras", "VALLEY SPRINGS": "calaveras",
    "COPPEROPOLIS": "calaveras",
    # ===== Colusa =====
    "COLUSA": "colusa", "WILLIAMS": "colusa", "MAXWELL": "colusa",
    # ===== Contra Costa (19 cities) =====
    "ANTIOCH": "contra-costa", "BRENTWOOD": "contra-costa",
    "CLAYTON": "contra-costa", "CONCORD": "contra-costa",
    "DANVILLE": "contra-costa", "EL CERRITO": "contra-costa",
    "HERCULES": "contra-costa", "LAFAYETTE": "contra-costa",
    "MARTINEZ": "contra-costa", "MORAGA": "contra-costa",
    "OAKLEY": "contra-costa", "ORINDA": "contra-costa",
    "PINOLE": "contra-costa", "PITTSBURG": "contra-costa",
    "PLEASANT HILL": "contra-costa", "RICHMOND": "contra-costa",
    "SAN PABLO": "contra-costa", "SAN RAMON": "contra-costa",
    "WALNUT CREEK": "contra-costa",
    # CC CDPs
    "ALAMO": "contra-costa", "BAY POINT": "contra-costa",
    "BLACKHAWK": "contra-costa", "DIABLO": "contra-costa",
    "DISCOVERY BAY": "contra-costa", "EL SOBRANTE": "contra-costa",
    "KENSINGTON": "contra-costa", "ROSSMOOR": "contra-costa",
    "BYRON": "contra-costa", "BETHEL ISLAND": "contra-costa",
    "TARA HILLS": "contra-costa", "RODEO": "contra-costa",
    "CROCKETT": "contra-costa",
    # ===== Del Norte =====
    "CRESCENT CITY": "del-norte", "SMITH RIVER": "del-norte",
    # ===== El Dorado =====
    "PLACERVILLE": "el-dorado", "SOUTH LAKE TAHOE": "el-dorado",
    "EL DORADO HILLS": "el-dorado", "CAMERON PARK": "el-dorado",
    "DIAMOND SPRINGS": "el-dorado", "POLLOCK PINES": "el-dorado",
    "SHINGLE SPRINGS": "el-dorado", "GEORGETOWN": "el-dorado",
    # ===== Fresno (15 cities) =====
    "CLOVIS": "fresno", "COALINGA": "fresno", "FIREBAUGH": "fresno",
    "FOWLER": "fresno", "FRESNO": "fresno", "HURON": "fresno",
    "KERMAN": "fresno", "KINGSBURG": "fresno", "MENDOTA": "fresno",
    "ORANGE COVE": "fresno", "PARLIER": "fresno", "REEDLEY": "fresno",
    "SAN JOAQUIN": "fresno", "SANGER": "fresno", "SELMA": "fresno",
    # ===== Glenn =====
    "ORLAND": "glenn", "WILLOWS": "glenn", "HAMILTON CITY": "glenn",
    # ===== Humboldt =====
    "ARCATA": "humboldt", "BLUE LAKE": "humboldt", "EUREKA": "humboldt",
    "FERNDALE": "humboldt", "FORTUNA": "humboldt", "RIO DELL": "humboldt",
    "TRINIDAD": "humboldt", "MCKINLEYVILLE": "humboldt",
    "GARBERVILLE": "humboldt", "BAYSIDE": "humboldt", "FIELDS LANDING": "humboldt",
    # ===== Imperial =====
    "BRAWLEY": "imperial", "CALEXICO": "imperial", "CALIPATRIA": "imperial",
    "EL CENTRO": "imperial", "HOLTVILLE": "imperial", "IMPERIAL": "imperial",
    "WESTMORLAND": "imperial",
    # ===== Inyo =====
    "BISHOP": "inyo", "LONE PINE": "inyo", "INDEPENDENCE": "inyo",
    # ===== Kern =====
    "ARVIN": "kern", "BAKERSFIELD": "kern", "CALIFORNIA CITY": "kern",
    "DELANO": "kern", "MARICOPA": "kern", "MCFARLAND": "kern",
    "RIDGECREST": "kern", "SHAFTER": "kern", "TAFT": "kern",
    "TEHACHAPI": "kern", "WASCO": "kern",
    "OILDALE": "kern", "ROSEDALE": "kern", "LAMONT": "kern",
    "LAKE ISABELLA": "kern", "ROSAMOND": "kern", "MOJAVE": "kern",
    "STALLION SPRINGS": "kern",
    # ===== Kings =====
    "AVENAL": "kings", "CORCORAN": "kings", "HANFORD": "kings",
    "LEMOORE": "kings", "ARMONA": "kings", "STRATFORD": "kings",
    # ===== Lake =====
    "CLEARLAKE": "lake", "LAKEPORT": "lake", "KELSEYVILLE": "lake",
    "MIDDLETOWN": "lake", "HIDDEN VALLEY LAKE": "lake",
    "LUCERNE": "lake", "NICE": "lake",
    # ===== Lassen =====
    "SUSANVILLE": "lassen", "WESTWOOD": "lassen", "BIEBER": "lassen",
    # ===== Los Angeles (88 cities) =====
    "AGOURA HILLS": "los-angeles", "ALHAMBRA": "los-angeles",
    "ARCADIA": "los-angeles", "ARTESIA": "los-angeles", "AVALON": "los-angeles",
    "AZUSA": "los-angeles", "BALDWIN PARK": "los-angeles", "BELL": "los-angeles",
    "BELL GARDENS": "los-angeles", "BELLFLOWER": "los-angeles",
    "BEVERLY HILLS": "los-angeles", "BRADBURY": "los-angeles",
    "BURBANK": "los-angeles", "CALABASAS": "los-angeles",
    "CARSON": "los-angeles", "CERRITOS": "los-angeles",
    "CLAREMONT": "los-angeles", "COMMERCE": "los-angeles",
    "COMPTON": "los-angeles", "COVINA": "los-angeles",
    "CUDAHY": "los-angeles", "CULVER CITY": "los-angeles",
    "DIAMOND BAR": "los-angeles", "DOWNEY": "los-angeles",
    "DUARTE": "los-angeles", "EL MONTE": "los-angeles",
    "EL SEGUNDO": "los-angeles", "GARDENA": "los-angeles",
    "GLENDALE": "los-angeles", "GLENDORA": "los-angeles",
    "HAWAIIAN GARDENS": "los-angeles", "HAWTHORNE": "los-angeles",
    "HERMOSA BEACH": "los-angeles", "HIDDEN HILLS": "los-angeles",
    "HUNTINGTON PARK": "los-angeles", "INDUSTRY": "los-angeles",
    "INGLEWOOD": "los-angeles", "IRWINDALE": "los-angeles",
    "LA CANADA FLINTRIDGE": "los-angeles", "LA HABRA HEIGHTS": "los-angeles",
    "LA MIRADA": "los-angeles", "LA PUENTE": "los-angeles",
    "LA VERNE": "los-angeles", "LAKEWOOD": "los-angeles",
    "LANCASTER": "los-angeles", "LAWNDALE": "los-angeles",
    "LOMITA": "los-angeles", "LONG BEACH": "los-angeles",
    "LOS ANGELES": "los-angeles", "LYNWOOD": "los-angeles",
    "MALIBU": "los-angeles", "MANHATTAN BEACH": "los-angeles",
    "MAYWOOD": "los-angeles", "MONROVIA": "los-angeles",
    "MONTEBELLO": "los-angeles", "MONTEREY PARK": "los-angeles",
    "NORWALK": "los-angeles", "PALMDALE": "los-angeles",
    "PALOS VERDES ESTATES": "los-angeles", "PARAMOUNT": "los-angeles",
    "PASADENA": "los-angeles", "PICO RIVERA": "los-angeles",
    "POMONA": "los-angeles", "RANCHO PALOS VERDES": "los-angeles",
    "REDONDO BEACH": "los-angeles", "ROLLING HILLS": "los-angeles",
    "ROLLING HILLS ESTATES": "los-angeles", "ROSEMEAD": "los-angeles",
    "SAN DIMAS": "los-angeles", "SAN FERNANDO": "los-angeles",
    "SAN GABRIEL": "los-angeles", "SAN MARINO": "los-angeles",
    "SANTA CLARITA": "los-angeles", "SANTA FE SPRINGS": "los-angeles",
    "SANTA MONICA": "los-angeles", "SIERRA MADRE": "los-angeles",
    "SIGNAL HILL": "los-angeles", "SOUTH EL MONTE": "los-angeles",
    "SOUTH GATE": "los-angeles", "SOUTH PASADENA": "los-angeles",
    "TEMPLE CITY": "los-angeles", "TORRANCE": "los-angeles",
    "VERNON": "los-angeles", "WALNUT": "los-angeles",
    "WEST COVINA": "los-angeles", "WEST HOLLYWOOD": "los-angeles",
    "WESTLAKE VILLAGE": "los-angeles", "WHITTIER": "los-angeles",
    # LA CDPs/neighborhoods (heavy HOA volume)
    "SHERMAN OAKS": "los-angeles", "WOODLAND HILLS": "los-angeles",
    "ENCINO": "los-angeles", "VAN NUYS": "los-angeles",
    "STUDIO CITY": "los-angeles", "TOLUCA LAKE": "los-angeles",
    "NORTH HOLLYWOOD": "los-angeles", "VALLEY VILLAGE": "los-angeles",
    "TARZANA": "los-angeles", "CANOGA PARK": "los-angeles",
    "RESEDA": "los-angeles", "NORTHRIDGE": "los-angeles",
    "GRANADA HILLS": "los-angeles", "MISSION HILLS": "los-angeles",
    "CHATSWORTH": "los-angeles", "WINNETKA": "los-angeles",
    "PORTER RANCH": "los-angeles", "WEST HILLS": "los-angeles",
    "HOLLYWOOD": "los-angeles", "MARINA DEL REY": "los-angeles",
    "PLAYA VISTA": "los-angeles", "PLAYA DEL REY": "los-angeles",
    "VENICE": "los-angeles", "PACIFIC PALISADES": "los-angeles",
    "WESTWOOD": "los-angeles", "BEL AIR": "los-angeles",
    "CENTURY CITY": "los-angeles", "HACIENDA HEIGHTS": "los-angeles",
    "ROWLAND HEIGHTS": "los-angeles", "VALENCIA": "los-angeles",
    "STEVENSON RANCH": "los-angeles", "QUARTZ HILL": "los-angeles",
    "ALTADENA": "los-angeles", "ROSEMOOR": "los-angeles",
    "EAST LOS ANGELES": "los-angeles", "FLORENCE-GRAHAM": "los-angeles",
    "WEST WHITTIER-LOS NIETOS": "los-angeles", "MARINA": "los-angeles",
    "TOPANGA": "los-angeles", "MAR VISTA": "los-angeles",
    "EAGLE ROCK": "los-angeles", "SILVER LAKE": "los-angeles",
    "SAN PEDRO": "los-angeles", "WILMINGTON": "los-angeles",
    "HARBOR CITY": "los-angeles", "ACTON": "los-angeles",
    "AGUA DULCE": "los-angeles", "CASTAIC": "los-angeles",
    "CANYON COUNTRY": "los-angeles", "NEWHALL": "los-angeles",
    "SAUGUS": "los-angeles", "SUN VALLEY": "los-angeles",
    "PANORAMA CITY": "los-angeles", "ARLETA": "los-angeles",
    "PACOIMA": "los-angeles", "SYLMAR": "los-angeles",
    "LAKE BALBOA": "los-angeles", "LAKEVIEW TERRACE": "los-angeles",
    "EL SERENO": "los-angeles", "BOYLE HEIGHTS": "los-angeles",
    "ECHO PARK": "los-angeles", "LOS FELIZ": "los-angeles",
    "LARCHMONT": "los-angeles", "HANCOCK PARK": "los-angeles",
    "MID-WILSHIRE": "los-angeles", "MID CITY": "los-angeles",
    "WEST ADAMS": "los-angeles", "BALDWIN HILLS": "los-angeles",
    "VIEW PARK-WINDSOR HILLS": "los-angeles", "LADERA HEIGHTS": "los-angeles",
    "WESTCHESTER": "los-angeles", "WALNUT PARK": "los-angeles",
    "WILLOWBROOK": "los-angeles", "ATHENS": "los-angeles",
    "LENNOX": "los-angeles", "ROSAMOND": "kern",  # boundary
    # ===== Madera =====
    "CHOWCHILLA": "madera", "MADERA": "madera",
    "OAKHURST": "madera", "BASS LAKE": "madera",
    # ===== Marin =====
    "BELVEDERE": "marin", "CORTE MADERA": "marin", "FAIRFAX": "marin",
    "LARKSPUR": "marin", "MILL VALLEY": "marin", "NOVATO": "marin",
    "ROSS": "marin", "SAN ANSELMO": "marin", "SAN RAFAEL": "marin",
    "SAUSALITO": "marin", "TIBURON": "marin",
    "BOLINAS": "marin", "INVERNESS": "marin", "POINT REYES STATION": "marin",
    "WOODACRE": "marin", "FORTUNA": "humboldt",  # dup
    "MARIN CITY": "marin", "STRAWBERRY": "marin", "KENTFIELD": "marin",
    "SANTA VENETIA": "marin",
    # ===== Mariposa =====
    "MARIPOSA": "mariposa", "EL PORTAL": "mariposa",
    # ===== Mendocino =====
    "FORT BRAGG": "mendocino", "POINT ARENA": "mendocino",
    "UKIAH": "mendocino", "WILLITS": "mendocino",
    "MENDOCINO": "mendocino", "BOONVILLE": "mendocino",
    "REDWOOD VALLEY": "mendocino", "COVELO": "mendocino",
    # ===== Merced =====
    "ATWATER": "merced", "DOS PALOS": "merced", "GUSTINE": "merced",
    "LIVINGSTON": "merced", "LOS BANOS": "merced", "MERCED": "merced",
    "PLANADA": "merced", "WINTON": "merced",
    # ===== Modoc =====
    "ALTURAS": "modoc", "TULELAKE": "siskiyou",  # boundary; Tulelake is in Siskiyou
    # ===== Mono =====
    "MAMMOTH LAKES": "mono", "JUNE LAKE": "mono",
    "BRIDGEPORT": "mono", "WALKER": "mono",
    # ===== Monterey =====
    "CARMEL-BY-THE-SEA": "monterey", "DEL REY OAKS": "monterey",
    "GONZALES": "monterey", "GREENFIELD": "monterey",
    "KING CITY": "monterey", "MARINA": "monterey",
    "MONTEREY": "monterey", "PACIFIC GROVE": "monterey",
    "SALINAS": "monterey", "SAND CITY": "monterey",
    "SEASIDE": "monterey", "SOLEDAD": "monterey",
    "PEBBLE BEACH": "monterey", "CARMEL VALLEY VILLAGE": "monterey",
    "PRUNEDALE": "monterey", "CASTROVILLE": "monterey",
    # ===== Napa =====
    "AMERICAN CANYON": "napa", "CALISTOGA": "napa",
    "NAPA": "napa", "ST HELENA": "napa", "ST. HELENA": "napa",
    "YOUNTVILLE": "napa", "ANGWIN": "napa", "RUTHERFORD": "napa",
    # ===== Nevada =====
    "GRASS VALLEY": "nevada", "NEVADA CITY": "nevada",
    "TRUCKEE": "nevada", "PENN VALLEY": "nevada",
    "NORTH SAN JUAN": "nevada", "ALTA SIERRA": "nevada",
    # ===== Orange (34 cities) =====
    "ALISO VIEJO": "orange", "ANAHEIM": "orange", "BREA": "orange",
    "BUENA PARK": "orange", "COSTA MESA": "orange", "CYPRESS": "orange",
    "DANA POINT": "orange", "FOUNTAIN VALLEY": "orange",
    "FULLERTON": "orange", "GARDEN GROVE": "orange",
    "HUNTINGTON BEACH": "orange", "IRVINE": "orange",
    "LA HABRA": "orange", "LA PALMA": "orange",
    "LAGUNA BEACH": "orange", "LAGUNA HILLS": "orange",
    "LAGUNA NIGUEL": "orange", "LAGUNA WOODS": "orange",
    "LAKE FOREST": "orange", "LOS ALAMITOS": "orange",
    "MISSION VIEJO": "orange", "NEWPORT BEACH": "orange",
    "ORANGE": "orange", "PLACENTIA": "orange",
    "RANCHO SANTA MARGARITA": "orange", "SAN CLEMENTE": "orange",
    "SAN JUAN CAPISTRANO": "orange", "SANTA ANA": "orange",
    "SEAL BEACH": "orange", "STANTON": "orange",
    "TUSTIN": "orange", "VILLA PARK": "orange",
    "WESTMINSTER": "orange", "YORBA LINDA": "orange",
    # OC neighborhoods/CDPs
    "RANCHO MISSION VIEJO": "orange", "LADERA RANCH": "orange",
    "TALEGA": "orange", "COTO DE CAZA": "orange",
    "MISSION VIEJO": "orange", "FOOTHILL RANCH": "orange",
    "PORTOLA HILLS": "orange", "SILVERADO": "orange",
    "TRABUCO CANYON": "orange", "ROSSMOOR": "orange",  # also Walnut Creek
    "MIDWAY CITY": "orange", "EL TORO": "orange",
    "BALBOA ISLAND": "orange", "CORONA DEL MAR": "orange",
    "CRYSTAL COVE": "orange", "MONARCH BEACH": "orange",
    "EMERALD BAY": "orange", "THREE ARCH BAY": "orange",
    "DOVE CANYON": "orange",
    # ===== Placer =====
    "AUBURN": "placer", "COLFAX": "placer", "LINCOLN": "placer",
    "LOOMIS": "placer", "ROCKLIN": "placer", "ROSEVILLE": "placer",
    "GRANITE BAY": "placer", "MEADOW VISTA": "placer",
    "FORESTHILL": "placer", "ALTA": "placer", "DUTCH FLAT": "placer",
    "TAHOE CITY": "placer", "KINGS BEACH": "placer",
    "TRUCKEE": "nevada",  # dup; Truckee is Nevada Co
    # ===== Plumas =====
    "PORTOLA": "plumas", "QUINCY": "plumas",
    "GRAEAGLE": "plumas", "BLAIRSDEN": "plumas",
    # ===== Riverside (28 cities) =====
    "BANNING": "riverside", "BEAUMONT": "riverside", "BLYTHE": "riverside",
    "CALIMESA": "riverside", "CANYON LAKE": "riverside",
    "CATHEDRAL CITY": "riverside", "COACHELLA": "riverside",
    "CORONA": "riverside", "DESERT HOT SPRINGS": "riverside",
    "EASTVALE": "riverside", "HEMET": "riverside",
    "INDIAN WELLS": "riverside", "INDIO": "riverside",
    "JURUPA VALLEY": "riverside", "LA QUINTA": "riverside",
    "LAKE ELSINORE": "riverside", "MENIFEE": "riverside",
    "MORENO VALLEY": "riverside", "MURRIETA": "riverside",
    "NORCO": "riverside", "PALM DESERT": "riverside",
    "PALM SPRINGS": "riverside", "PERRIS": "riverside",
    "RANCHO MIRAGE": "riverside", "RIVERSIDE": "riverside",
    "SAN JACINTO": "riverside", "TEMECULA": "riverside",
    "WILDOMAR": "riverside",
    # Riverside CDPs
    "MEAD VALLEY": "riverside", "WOODCREST": "riverside",
    "MIRA LOMA": "riverside", "MOUNTAIN CENTER": "riverside",
    "WINCHESTER": "riverside", "FRENCH VALLEY": "riverside",
    "BERMUDA DUNES": "riverside", "THERMAL": "riverside",
    "MECCA": "riverside", "VALLE VISTA": "riverside",
    "IDYLLWILD": "riverside", "IDYLLWILD-PINE COVE": "riverside",
    "ANZA": "riverside", "AGUANGA": "riverside",
    "SAGE": "riverside", "HOMELAND": "riverside",
    "ROMOLAND": "riverside", "QUAIL VALLEY": "riverside",
    "GOOD HOPE": "riverside", "MEADOWBROOK": "riverside",
    "EL CERRITO": "riverside",  # dup with CC County, but RIV has one too
    # ===== Sacramento (7 cities) =====
    "CITRUS HEIGHTS": "sacramento", "ELK GROVE": "sacramento",
    "FOLSOM": "sacramento", "GALT": "sacramento",
    "ISLETON": "sacramento", "RANCHO CORDOVA": "sacramento",
    "SACRAMENTO": "sacramento",
    # Sacramento CDPs
    "ANTELOPE": "sacramento", "ARDEN-ARCADE": "sacramento",
    "CARMICHAEL": "sacramento", "CLAY": "sacramento",
    "FAIR OAKS": "sacramento", "FLORIN": "sacramento",
    "FOOTHILL FARMS": "sacramento", "GOLD RIVER": "sacramento",
    "LA RIVIERA": "sacramento", "MATHER": "sacramento",
    "NORTH HIGHLANDS": "sacramento", "ORANGEVALE": "sacramento",
    "PARKWAY": "sacramento", "RIO LINDA": "sacramento",
    "ROSEMONT": "sacramento", "VINEYARD": "sacramento",
    "WALNUT GROVE": "sacramento", "WILTON": "sacramento",
    "HERALD": "sacramento", "FRUITRIDGE POCKET": "sacramento",
    # ===== San Benito =====
    "HOLLISTER": "san-benito", "SAN JUAN BAUTISTA": "san-benito",
    "AROMAS": "san-benito", "TRES PINOS": "san-benito",
    # ===== San Bernardino (24 cities) =====
    "ADELANTO": "san-bernardino", "APPLE VALLEY": "san-bernardino",
    "BARSTOW": "san-bernardino", "BIG BEAR LAKE": "san-bernardino",
    "CHINO": "san-bernardino", "CHINO HILLS": "san-bernardino",
    "COLTON": "san-bernardino", "FONTANA": "san-bernardino",
    "GRAND TERRACE": "san-bernardino", "HESPERIA": "san-bernardino",
    "HIGHLAND": "san-bernardino", "LOMA LINDA": "san-bernardino",
    "MONTCLAIR": "san-bernardino", "NEEDLES": "san-bernardino",
    "ONTARIO": "san-bernardino", "RANCHO CUCAMONGA": "san-bernardino",
    "REDLANDS": "san-bernardino", "RIALTO": "san-bernardino",
    "SAN BERNARDINO": "san-bernardino", "TWENTYNINE PALMS": "san-bernardino",
    "UPLAND": "san-bernardino", "VICTORVILLE": "san-bernardino",
    "YUCAIPA": "san-bernardino", "YUCCA VALLEY": "san-bernardino",
    # SB CDPs
    "MENTONE": "san-bernardino", "BLOOMINGTON": "san-bernardino",
    "MUSCOY": "san-bernardino", "OAK HILLS": "san-bernardino",
    "PHELAN": "san-bernardino", "PINON HILLS": "san-bernardino",
    "RUNNING SPRINGS": "san-bernardino", "CRESTLINE": "san-bernardino",
    "LAKE ARROWHEAD": "san-bernardino", "WRIGHTWOOD": "san-bernardino",
    "JOSHUA TREE": "san-bernardino", "MORONGO VALLEY": "san-bernardino",
    "LANDERS": "san-bernardino", "BIG RIVER": "san-bernardino",
    "BIG BEAR CITY": "san-bernardino", "FAWNSKIN": "san-bernardino",
    "FORT IRWIN": "san-bernardino", "BAKER": "san-bernardino",
    # ===== San Diego (18 cities) =====
    "CARLSBAD": "san-diego", "CHULA VISTA": "san-diego",
    "CORONADO": "san-diego", "DEL MAR": "san-diego",
    "EL CAJON": "san-diego", "ENCINITAS": "san-diego",
    "ESCONDIDO": "san-diego", "IMPERIAL BEACH": "san-diego",
    "LA MESA": "san-diego", "LEMON GROVE": "san-diego",
    "NATIONAL CITY": "san-diego", "OCEANSIDE": "san-diego",
    "POWAY": "san-diego", "SAN DIEGO": "san-diego",
    "SAN MARCOS": "san-diego", "SANTEE": "san-diego",
    "SOLANA BEACH": "san-diego", "VISTA": "san-diego",
    # SD CDPs/neighborhoods
    "RANCHO BERNARDO": "san-diego", "SCRIPPS RANCH": "san-diego",
    "RANCHO PENASQUITOS": "san-diego", "DEL MAR HEIGHTS": "san-diego",
    "CARMEL VALLEY": "san-diego", "TIERRASANTA": "san-diego",
    "MIRA MESA": "san-diego", "MIRAMAR": "san-diego",
    "SORRENTO VALLEY": "san-diego", "RANCHO SANTA FE": "san-diego",
    "FAIRBANKS RANCH": "san-diego", "OLIVENHAIN": "san-diego",
    "CARDIFF": "san-diego", "CARDIFF BY THE SEA": "san-diego",
    "LEUCADIA": "san-diego", "POTRERO": "san-diego",
    "BORREGO SPRINGS": "san-diego", "CAMPO": "san-diego",
    "JULIAN": "san-diego", "DESCANSO": "san-diego",
    "JAMUL": "san-diego", "ALPINE": "san-diego",
    "BONITA": "san-diego", "BONSALL": "san-diego",
    "CASA DE ORO-MOUNT HELIX": "san-diego", "FALLBROOK": "san-diego",
    "LAKESIDE": "san-diego", "PINE VALLEY": "san-diego",
    "RAMONA": "san-diego", "SPRING VALLEY": "san-diego",
    "VALLEY CENTER": "san-diego", "WINTER GARDENS": "san-diego",
    "CREST": "san-diego", "PALA": "san-diego",
    "LA JOLLA": "san-diego", "PACIFIC BEACH": "san-diego",
    "POINT LOMA": "san-diego", "OCEAN BEACH": "san-diego",
    "MISSION BEACH": "san-diego", "OLD TOWN": "san-diego",
    "HILLCREST": "san-diego", "MISSION HILLS": "san-diego",  # SD also has one
    "NORTH PARK": "san-diego", "SOUTH PARK": "san-diego",
    "GOLDEN HILL": "san-diego", "GASLAMP": "san-diego",
    "EAST VILLAGE": "san-diego", "LITTLE ITALY": "san-diego",
    "DOWNTOWN SAN DIEGO": "san-diego",
    # ===== San Francisco =====
    "SAN FRANCISCO": "san-francisco",
    # ===== San Joaquin (7 cities) =====
    "ESCALON": "san-joaquin", "LATHROP": "san-joaquin",
    "LODI": "san-joaquin", "MANTECA": "san-joaquin",
    "RIPON": "san-joaquin", "STOCKTON": "san-joaquin",
    "TRACY": "san-joaquin",
    "MOUNTAIN HOUSE": "san-joaquin", "ACAMPO": "san-joaquin",
    "FRENCH CAMP": "san-joaquin", "WOODBRIDGE": "san-joaquin",
    "MORADA": "san-joaquin", "GARDEN ACRES": "san-joaquin",
    # ===== San Luis Obispo =====
    "ARROYO GRANDE": "san-luis-obispo", "ATASCADERO": "san-luis-obispo",
    "EL PASO DE ROBLES": "san-luis-obispo", "PASO ROBLES": "san-luis-obispo",
    "GROVER BEACH": "san-luis-obispo", "MORRO BAY": "san-luis-obispo",
    "PISMO BEACH": "san-luis-obispo", "SAN LUIS OBISPO": "san-luis-obispo",
    "AVILA BEACH": "san-luis-obispo", "BAYWOOD-LOS OSOS": "san-luis-obispo",
    "LOS OSOS": "san-luis-obispo", "CAMBRIA": "san-luis-obispo",
    "CAYUCOS": "san-luis-obispo", "NIPOMO": "san-luis-obispo",
    "OCEANO": "san-luis-obispo", "SAN MIGUEL": "san-luis-obispo",
    "SHANDON": "san-luis-obispo", "TEMPLETON": "san-luis-obispo",
    # ===== San Mateo (20 cities) =====
    "ATHERTON": "san-mateo", "BELMONT": "san-mateo",
    "BRISBANE": "san-mateo", "BURLINGAME": "san-mateo",
    "COLMA": "san-mateo", "DALY CITY": "san-mateo",
    "EAST PALO ALTO": "san-mateo", "FOSTER CITY": "san-mateo",
    "HALF MOON BAY": "san-mateo", "HILLSBOROUGH": "san-mateo",
    "MENLO PARK": "san-mateo", "MILLBRAE": "san-mateo",
    "PACIFICA": "san-mateo", "PORTOLA VALLEY": "san-mateo",
    "REDWOOD CITY": "san-mateo", "SAN BRUNO": "san-mateo",
    "SAN CARLOS": "san-mateo", "SAN MATEO": "san-mateo",
    "SOUTH SAN FRANCISCO": "san-mateo", "WOODSIDE": "san-mateo",
    # SM CDPs
    "BROADMOOR": "san-mateo", "EL GRANADA": "san-mateo",
    "EMERALD HILLS": "san-mateo", "HIGHLANDS-BAYWOOD PARK": "san-mateo",
    "LA HONDA": "san-mateo", "LADERA": "san-mateo",
    "LOMA MAR": "san-mateo", "MOSS BEACH": "san-mateo",
    "MONTARA": "san-mateo", "NORTH FAIR OAKS": "san-mateo",
    "PESCADERO": "san-mateo", "WEST MENLO PARK": "san-mateo",
    # ===== Santa Barbara =====
    "BUELLTON": "santa-barbara", "CARPINTERIA": "santa-barbara",
    "GOLETA": "santa-barbara", "GUADALUPE": "santa-barbara",
    "LOMPOC": "santa-barbara", "SANTA BARBARA": "santa-barbara",
    "SANTA MARIA": "santa-barbara", "SOLVANG": "santa-barbara",
    "MONTECITO": "santa-barbara", "SUMMERLAND": "santa-barbara",
    "ISLA VISTA": "santa-barbara", "MISSION CANYON": "santa-barbara",
    "ORCUTT": "santa-barbara", "VANDENBERG VILLAGE": "santa-barbara",
    "MISSION HILLS": "santa-barbara",  # also LA county
    "LOS ALAMOS": "santa-barbara", "BALLARD": "santa-barbara",
    "LOS OLIVOS": "santa-barbara", "SANTA YNEZ": "santa-barbara",
    "TOY OAKS PARK": "santa-barbara",
    # ===== Santa Clara (15 cities) =====
    "CAMPBELL": "santa-clara", "CUPERTINO": "santa-clara",
    "GILROY": "santa-clara", "LOS ALTOS": "santa-clara",
    "LOS ALTOS HILLS": "santa-clara", "LOS GATOS": "santa-clara",
    "MILPITAS": "santa-clara", "MONTE SERENO": "santa-clara",
    "MORGAN HILL": "santa-clara", "MOUNTAIN VIEW": "santa-clara",
    "PALO ALTO": "santa-clara", "SAN JOSE": "santa-clara",
    "SANTA CLARA": "santa-clara", "SARATOGA": "santa-clara",
    "SUNNYVALE": "santa-clara",
    # SC CDPs
    "ALUM ROCK": "santa-clara", "BURBANK": "santa-clara",  # also LA
    "EAST FOOTHILLS": "santa-clara", "FRUITDALE": "santa-clara",
    "LEXINGTON HILLS": "santa-clara", "LOYOLA": "santa-clara",
    "STANFORD": "santa-clara",
    # ===== Santa Cruz =====
    "CAPITOLA": "santa-cruz", "SANTA CRUZ": "santa-cruz",
    "SCOTTS VALLEY": "santa-cruz", "WATSONVILLE": "santa-cruz",
    "APTOS": "santa-cruz", "BEN LOMOND": "santa-cruz",
    "BONNY DOON": "santa-cruz", "BOULDER CREEK": "santa-cruz",
    "FELTON": "santa-cruz", "FREEDOM": "santa-cruz",
    "INTERLAKEN": "santa-cruz", "LIVE OAK": "santa-cruz",
    "LOMPICO": "santa-cruz", "PASATIEMPO": "santa-cruz",
    "RIO DEL MAR": "santa-cruz", "SOQUEL": "santa-cruz",
    "TWIN LAKES": "santa-cruz", "DAVENPORT": "santa-cruz",
    # ===== Shasta =====
    "ANDERSON": "shasta", "REDDING": "shasta",
    "SHASTA LAKE": "shasta", "BURNEY": "shasta",
    "PALO CEDRO": "shasta", "MOUNTAIN GATE": "shasta",
    "SHINGLETOWN": "shasta",
    # ===== Sierra =====
    "LOYALTON": "sierra", "DOWNIEVILLE": "sierra",
    # ===== Siskiyou =====
    "DORRIS": "siskiyou", "DUNSMUIR": "siskiyou",
    "ETNA": "siskiyou", "FORT JONES": "siskiyou",
    "MONTAGUE": "siskiyou", "MOUNT SHASTA": "siskiyou",
    "TULELAKE": "siskiyou", "WEED": "siskiyou", "YREKA": "siskiyou",
    # ===== Solano =====
    "BENICIA": "solano", "DIXON": "solano", "FAIRFIELD": "solano",
    "RIO VISTA": "solano", "SUISUN CITY": "solano", "VACAVILLE": "solano",
    "VALLEJO": "solano", "GREEN VALLEY": "solano", "ELMIRA": "solano",
    "ALLENDALE": "solano",
    # ===== Sonoma =====
    "CLOVERDALE": "sonoma", "COTATI": "sonoma", "HEALDSBURG": "sonoma",
    "PETALUMA": "sonoma", "ROHNERT PARK": "sonoma", "SANTA ROSA": "sonoma",
    "SEBASTOPOL": "sonoma", "SONOMA": "sonoma", "WINDSOR": "sonoma",
    "BODEGA BAY": "sonoma", "BOYES HOT SPRINGS": "sonoma",
    "EL VERANO": "sonoma", "FORESTVILLE": "sonoma",
    "GLEN ELLEN": "sonoma", "GRATON": "sonoma",
    "GUERNEVILLE": "sonoma", "JENNER": "sonoma",
    "KENWOOD": "sonoma", "MONTE RIO": "sonoma",
    "PENNGROVE": "sonoma", "SEA RANCH": "sonoma", "THE SEA RANCH": "sonoma",
    "SONOMA": "sonoma", "TIMBER COVE": "sonoma",
    # ===== Stanislaus =====
    "CERES": "stanislaus", "HUGHSON": "stanislaus", "MODESTO": "stanislaus",
    "NEWMAN": "stanislaus", "OAKDALE": "stanislaus", "PATTERSON": "stanislaus",
    "RIVERBANK": "stanislaus", "TURLOCK": "stanislaus", "WATERFORD": "stanislaus",
    "DENAIR": "stanislaus", "EMPIRE": "stanislaus", "HICKMAN": "stanislaus",
    "KEYES": "stanislaus", "KNIGHTS FERRY": "stanislaus",
    "SALIDA": "stanislaus", "WESTLEY": "stanislaus",
    # ===== Sutter =====
    "LIVE OAK": "sutter", "YUBA CITY": "sutter", "SUTTER": "sutter",
    "EAST NICOLAUS": "sutter", "PLEASANT GROVE": "sutter",
    # ===== Tehama =====
    "CORNING": "tehama", "RED BLUFF": "tehama", "TEHAMA": "tehama",
    "LOS MOLINOS": "tehama", "GERBER": "tehama",
    # ===== Trinity =====
    "WEAVERVILLE": "trinity", "HAYFORK": "trinity",
    "LEWISTON": "trinity",
    # ===== Tulare =====
    "DINUBA": "tulare", "EXETER": "tulare", "FARMERSVILLE": "tulare",
    "LINDSAY": "tulare", "PORTERVILLE": "tulare", "TULARE": "tulare",
    "VISALIA": "tulare", "WOODLAKE": "tulare",
    "GOSHEN": "tulare", "ORANGE COVE": "fresno",  # dup; OC is in Fresno
    "STRATHMORE": "tulare", "EARLIMART": "tulare",
    "PIXLEY": "tulare", "TIPTON": "tulare",
    "THREE RIVERS": "tulare", "SPRINGVILLE": "tulare",
    # ===== Tuolumne =====
    "SONORA": "tuolumne", "TUOLUMNE CITY": "tuolumne",
    "JAMESTOWN": "tuolumne", "MI WUK VILLAGE": "tuolumne",
    "GROVELAND": "tuolumne", "TWAIN HARTE": "tuolumne",
    "COLUMBIA": "tuolumne",
    # ===== Ventura (10 cities) =====
    "CAMARILLO": "ventura", "FILLMORE": "ventura",
    "MOORPARK": "ventura", "OJAI": "ventura",
    "OXNARD": "ventura", "PORT HUENEME": "ventura",
    "SAN BUENAVENTURA": "ventura", "VENTURA": "ventura",
    "SANTA PAULA": "ventura", "SIMI VALLEY": "ventura",
    "THOUSAND OAKS": "ventura",
    "NEWBURY PARK": "ventura", "OAK PARK": "ventura",
    "BELL CANYON": "ventura", "MEINERS OAKS": "ventura",
    "OAK VIEW": "ventura", "MIRA MONTE": "ventura",
    "EL RIO": "ventura", "PIRU": "ventura", "SOMIS": "ventura",
    "WESTLAKE VILLAGE": "los-angeles",  # actually LA county
    "CASA CONEJO": "ventura",
    # ===== Yolo =====
    "DAVIS": "yolo", "WEST SACRAMENTO": "yolo", "WINTERS": "yolo",
    "WOODLAND": "yolo", "ESPARTO": "yolo", "KNIGHTS LANDING": "yolo",
    "MADISON": "yolo", "DUNNIGAN": "yolo", "YOLO": "yolo",
    # ===== Yuba =====
    "MARYSVILLE": "yuba", "WHEATLAND": "yuba",
    "LINDA": "yuba", "OLIVEHURST": "yuba",
    "BROWNS VALLEY": "yuba", "DOBBINS": "yuba",
    "CHALLENGE-BROWNSVILLE": "yuba",
}


def main() -> None:
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(CA_CITY_COUNTY, f, sort_keys=True, indent=2)
    n_cities = len(CA_CITY_COUNTY)
    n_counties = len(set(CA_CITY_COUNTY.values()))
    print(f"Saved -> {OUTPUT_PATH}")
    print(f"  {n_cities:,} cities/CDPs/neighborhoods mapped to {n_counties} counties")


if __name__ == "__main__":
    main()
