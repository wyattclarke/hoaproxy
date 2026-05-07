"""Indiana geography helpers — CITY_COUNTY mapping + city centroids.

Indiana has 92 counties. The CITY_COUNTY map covers incorporated places and a
handful of common postal-village / unincorporated-CDP names that show up on
deed restrictions and HOA filings but don't cleanly match a town. Centroids
are best-effort (rough city center, WGS84) — used only as a fallback when
ZIP-centroid lookup misses.

Sources: USPS / OpenStreetMap consensus city centers cross-checked against
Indiana Geographic Information Office boundaries (lat/lon ~1-2 mi precision).
"""

from __future__ import annotations

# Bounding box covers all 92 counties + a small buffer for ZIP centroid noise
# at borders. Indiana proper: 37.77N-41.76N, 87.51W-84.78W.
IN_BBOX = {"min_lat": 37.70, "max_lat": 41.80, "min_lon": -88.10, "max_lon": -84.70}


# Lower-cased city → county. Includes incorporated cities/towns + common
# unincorporated CDPs and postal-place names. When two states have the same
# city name, the IN entry wins here because we only call this with leads
# already classified as IN.
CITY_COUNTY: dict[str, str] = {
    # Marion County (Indianapolis + townships)
    "indianapolis": "Marion", "lawrence": "Marion", "speedway": "Marion",
    "beech grove": "Marion", "southport": "Marion", "warren park": "Marion",
    "spring hill": "Marion", "wynnedale": "Marion", "rocky ripple": "Marion",
    "homecroft": "Marion", "meridian hills": "Marion", "north crows nest": "Marion",
    "crows nest": "Marion", "williams creek": "Marion", "clermont": "Marion",
    "geist": "Marion", "castleton": "Marion", "broad ripple": "Marion",
    "irvington": "Marion",
    # Hamilton County
    "carmel": "Hamilton", "fishers": "Hamilton", "noblesville": "Hamilton",
    "westfield": "Hamilton", "cicero": "Hamilton", "atlanta": "Hamilton",
    "sheridan": "Hamilton", "arcadia": "Hamilton",
    # Hendricks County
    "avon": "Hendricks", "brownsburg": "Hendricks", "plainfield": "Hendricks",
    "danville": "Hendricks", "pittsboro": "Hendricks", "north salem": "Hendricks",
    "lizton": "Hendricks", "coatesville": "Hendricks", "amo": "Hendricks",
    "stilesville": "Hendricks", "clayton": "Hendricks",
    # Johnson County
    "greenwood": "Johnson", "franklin": "Johnson", "bargersville": "Johnson",
    "whiteland": "Johnson", "new whiteland": "Johnson", "edinburgh": "Johnson",
    "trafalgar": "Johnson", "prince's lakes": "Johnson", "princes lakes": "Johnson",
    "center grove": "Johnson", "needham": "Johnson",
    # Boone County
    "zionsville": "Boone", "lebanon": "Boone", "whitestown": "Boone",
    "thorntown": "Boone", "jamestown": "Boone", "advance": "Boone",
    "ulen": "Boone",
    # Lake County
    "crown point": "Lake", "munster": "Lake", "schererville": "Lake",
    "highland": "Lake", "hammond": "Lake", "gary": "Lake", "merrillville": "Lake",
    "cedar lake": "Lake", "st. john": "Lake", "saint john": "Lake", "st john": "Lake",
    "dyer": "Lake", "griffith": "Lake", "lowell": "Lake", "whiting": "Lake",
    "east chicago": "Lake", "winfield": "Lake", "hobart": "Lake",
    "lake station": "Lake", "new chicago": "Lake",
    # Porter County
    "valparaiso": "Porter", "portage": "Porter", "chesterton": "Porter",
    "porter": "Porter", "burns harbor": "Porter", "hebron": "Porter",
    "kouts": "Porter", "ogden dunes": "Porter", "south haven": "Porter",
    "beverly shores": "Porter", "the pines": "Porter", "pines": "Porter",
    "dune acres": "Porter",
    # Allen County
    "fort wayne": "Allen", "ft. wayne": "Allen", "ft wayne": "Allen",
    "new haven": "Allen", "huntertown": "Allen", "leo-cedarville": "Allen",
    "leo": "Allen", "cedarville": "Allen", "grabill": "Allen", "woodburn": "Allen",
    "monroeville": "Allen", "zanesville": "Allen", "aboite": "Allen",
    # St. Joseph County
    "south bend": "St. Joseph", "mishawaka": "St. Joseph", "granger": "St. Joseph",
    "osceola": "St. Joseph", "walkerton": "St. Joseph", "north liberty": "St. Joseph",
    "new carlisle": "St. Joseph", "lakeville": "St. Joseph", "indian village": "St. Joseph",
    "roseland": "St. Joseph", "notre dame": "St. Joseph",
    # Elkhart County
    "elkhart": "Elkhart", "goshen": "Elkhart", "bristol": "Elkhart",
    "middlebury": "Elkhart", "nappanee": "Elkhart", "wakarusa": "Elkhart",
    "millersburg": "Elkhart", "new paris": "Elkhart",
    # Tippecanoe County
    "lafayette": "Tippecanoe", "west lafayette": "Tippecanoe",
    "battle ground": "Tippecanoe", "dayton": "Tippecanoe", "otterbein": "Tippecanoe",
    "shadeland": "Tippecanoe",
    # Madison County (Indiana)
    "anderson": "Madison", "pendleton": "Madison", "elwood": "Madison",
    "alexandria": "Madison", "lapel": "Madison", "ingalls": "Madison",
    "frankton": "Madison", "summitville": "Madison", "orestes": "Madison",
    "edgewood": "Madison",
    # Vanderburgh County
    "evansville": "Vanderburgh", "darmstadt": "Vanderburgh",
    # Monroe County (Indiana)
    "bloomington": "Monroe", "ellettsville": "Monroe", "stinesville": "Monroe",
    # Clark County (Indiana)
    "jeffersonville": "Clark", "clarksville": "Clark", "charlestown": "Clark",
    "sellersburg": "Clark", "henryville": "Clark", "borden": "Clark",
    "memphis": "Clark",
    # Hancock County
    "greenfield": "Hancock", "mccordsville": "Hancock", "fortville": "Hancock",
    "new palestine": "Hancock", "cumberland": "Hancock", "shirley": "Hancock",
    "wilkinson": "Hancock",
    # Morgan County (Indiana)
    "mooresville": "Morgan", "martinsville": "Morgan", "monrovia": "Morgan",
    "morgantown": "Morgan", "paragon": "Morgan",
    # Warrick County
    "newburgh": "Warrick", "boonville": "Warrick", "chandler": "Warrick",
    "elberfeld": "Warrick", "tennyson": "Warrick", "lynnville": "Warrick",
    # Bartholomew County
    "columbus": "Bartholomew", "hope": "Bartholomew", "elizabethtown": "Bartholomew",
    "hartsville": "Bartholomew", "jonesville": "Bartholomew",
    # Howard County
    "kokomo": "Howard", "russiaville": "Howard", "greentown": "Howard",
    # Vigo County
    "terre haute": "Vigo", "west terre haute": "Vigo", "seelyville": "Vigo",
    "riley": "Vigo",
    # Delaware County
    "muncie": "Delaware", "yorktown": "Delaware", "selma": "Delaware",
    "eaton": "Delaware", "albany": "Delaware", "daleville": "Delaware",
    "gaston": "Delaware",
    # LaPorte County
    "la porte": "LaPorte", "laporte": "LaPorte", "michigan city": "LaPorte",
    "long beach": "LaPorte", "kingsbury": "LaPorte", "westville": "LaPorte",
    "rolling prairie": "LaPorte", "trail creek": "LaPorte",
    # Wayne County (Indiana)
    "richmond": "Wayne", "centerville": "Wayne", "cambridge city": "Wayne",
    "hagerstown": "Wayne", "fountain city": "Wayne", "milton": "Wayne",
    "boston": "Wayne", "economy": "Wayne",
    # Kosciusko County
    "warsaw": "Kosciusko", "winona lake": "Kosciusko", "syracuse": "Kosciusko",
    "milford": "Kosciusko", "mentone": "Kosciusko", "north webster": "Kosciusko",
    "pierceton": "Kosciusko", "claypool": "Kosciusko", "leesburg": "Kosciusko",
    "silver lake": "Kosciusko", "burket": "Kosciusko",
    # Floyd County
    "new albany": "Floyd", "georgetown": "Floyd", "greenville": "Floyd",
    # Grant County
    "marion": "Grant", "gas city": "Grant", "upland": "Grant",
    "fairmount": "Grant", "jonesboro": "Grant", "matthews": "Grant",
    "swayzee": "Grant", "van buren": "Grant",
    # DuBois County
    "jasper": "DuBois", "huntingburg": "DuBois", "ferdinand": "DuBois",
    "birdseye": "DuBois", "holland": "DuBois", "ireland": "DuBois",
    # Shelby County (Indiana)
    "shelbyville": "Shelby", "morristown": "Shelby", "fairland": "Shelby",
    "st. paul": "Shelby", "saint paul": "Shelby",
    # Cass County (Indiana)
    "logansport": "Cass", "galveston": "Cass", "royal center": "Cass",
    "walton": "Cass",
    # Henry County (Indiana)
    "new castle": "Henry", "knightstown": "Henry", "middletown": "Henry",
    "lewisville": "Henry", "spiceland": "Henry", "straughn": "Henry",
    "blountsville": "Henry",
    # Steuben County
    "angola": "Steuben", "fremont": "Steuben", "ashley": "Steuben",
    "hamilton": "Steuben", "hudson": "Steuben", "orland": "Steuben",
    # Whitley County
    "columbia city": "Whitley", "churubusco": "Whitley", "south whitley": "Whitley",
    "larwill": "Whitley",
    # Noble County
    "kendallville": "Noble", "ligonier": "Noble", "albion": "Noble",
    "rome city": "Noble", "wolcottville": "Noble", "avilla": "Noble",
    # DeKalb County (Indiana)
    "auburn": "DeKalb", "garrett": "DeKalb", "butler": "DeKalb",
    "waterloo": "DeKalb", "st. joe": "DeKalb",
    # Wabash County
    "wabash": "Wabash", "north manchester": "Wabash", "roann": "Wabash",
    "lagro": "Wabash", "lafontaine": "Wabash",
    # Marshall County
    "plymouth": "Marshall", "bremen": "Marshall", "argos": "Marshall",
    "bourbon": "Marshall", "culver": "Marshall", "lapaz": "Marshall",
    "la paz": "Marshall",
    # Fulton County (Indiana)
    "rochester": "Fulton", "akron": "Fulton", "fulton": "Fulton",
    "kewanna": "Fulton",
    # Knox County (Indiana)
    "vincennes": "Knox", "bicknell": "Knox", "monroe city": "Knox",
    "oaktown": "Knox", "edwardsport": "Knox", "westphalia": "Knox",
    "wheatland": "Knox",
    # Lawrence County (Indiana)
    "bedford": "Lawrence", "mitchell": "Lawrence", "oolitic": "Lawrence",
    "springville": "Lawrence",
    # Greene County (Indiana)
    "linton": "Greene", "bloomfield": "Greene", "jasonville": "Greene",
    "switz city": "Greene", "lyons": "Greene", "worthington": "Greene",
    # Brown County (Indiana)
    "nashville": "Brown", "helmsburg": "Brown",
    # Owen County
    "spencer": "Owen", "gosport": "Owen", "patricksburg": "Owen",
    # Posey County
    "mount vernon": "Posey", "poseyville": "Posey", "new harmony": "Posey",
    "cynthiana": "Posey",
    # Gibson County
    "princeton": "Gibson", "fort branch": "Gibson", "oakland city": "Gibson",
    "haubstadt": "Gibson", "owensville": "Gibson", "francisco": "Gibson",
    "patoka": "Gibson",
    # Pike County (Indiana)
    "petersburg": "Pike", "winslow": "Pike", "spurgeon": "Pike",
    # Daviess County (Indiana)
    "washington": "Daviess", "elnora": "Daviess", "odon": "Daviess",
    "montgomery": "Daviess",
    # Martin County
    "loogootee": "Martin", "shoals": "Martin", "crane": "Martin",
    # Orange County (Indiana)
    "paoli": "Orange", "french lick": "Orange", "west baden springs": "Orange",
    "orleans": "Orange",
    # Crawford County (Indiana)
    "english": "Crawford", "marengo": "Crawford", "milltown": "Crawford",
    # Harrison County (Indiana)
    "corydon": "Harrison", "lanesville": "Harrison", "elizabeth": "Harrison",
    "new salisbury": "Harrison", "palmyra": "Harrison",
    # Washington County (Indiana)
    "salem": "Washington", "campbellsburg": "Washington", "hardinsburg": "Washington",
    "pekin": "Washington", "saltillo": "Washington",
    # Scott County (Indiana)
    "scottsburg": "Scott", "austin": "Scott",
    # Jefferson County (Indiana)
    "madison": "Jefferson", "hanover": "Jefferson", "dupont": "Jefferson",
    # Switzerland County
    "vevay": "Switzerland", "patriot": "Switzerland",
    # Ohio County (Indiana)
    "rising sun": "Ohio",
    # Dearborn County
    "lawrenceburg": "Dearborn", "aurora": "Dearborn", "greendale": "Dearborn",
    "dillsboro": "Dearborn", "moores hill": "Dearborn", "west harrison": "Dearborn",
    # Ripley County
    "versailles": "Ripley", "batesville": "Ripley", "milan": "Ripley",
    "osgood": "Ripley", "sunman": "Ripley", "napoleon": "Ripley",
    # Decatur County (Indiana)
    "greensburg": "Decatur",
    # Franklin County (Indiana)
    "brookville": "Franklin", "laurel": "Franklin", "metamora": "Franklin",
    # Union County (Indiana)
    "liberty": "Union",
    # Fayette County (Indiana)
    "connersville": "Fayette",
    # Rush County
    "rushville": "Rush", "milroy": "Rush",
    # Randolph County (Indiana)
    "winchester": "Randolph", "union city": "Randolph", "lynn": "Randolph",
    "farmland": "Randolph", "modoc": "Randolph",
    # Jay County
    "portland": "Jay", "dunkirk": "Jay", "redkey": "Jay", "pennville": "Jay",
    # Adams County (Indiana)
    "decatur": "Adams", "geneva": "Adams", "berne": "Adams", "monroe": "Adams",
    # Wells County
    "bluffton": "Wells", "ossian": "Wells", "uniondale": "Wells",
    "markle": "Wells",
    # Huntington County
    "huntington": "Huntington", "andrews": "Huntington", "roanoke": "Huntington",
    "warren": "Huntington",
    # Blackford County
    "hartford city": "Blackford", "montpelier": "Blackford",
    # Miami County (Indiana)
    "peru": "Miami", "denver": "Miami", "macy": "Miami", "amboy": "Miami",
    # Tipton County
    "tipton": "Tipton", "windfall": "Tipton", "sharpsville": "Tipton",
    # Clinton County (Indiana)
    "frankfort": "Clinton", "kirklin": "Clinton", "rossville": "Clinton",
    "michigantown": "Clinton", "mulberry": "Clinton",
    # Carroll County (Indiana)
    "delphi": "Carroll", "flora": "Carroll", "camden": "Carroll",
    # White County (Indiana)
    "monticello": "White", "monon": "White", "wolcott": "White",
    "brookston": "White", "reynolds": "White", "chalmers": "White",
    # Pulaski County (Indiana)
    "winamac": "Pulaski", "francesville": "Pulaski", "medaryville": "Pulaski",
    # Starke County
    "knox": "Starke", "north judson": "Starke", "hamlet": "Starke",
    # Jasper County (Indiana)
    "rensselaer": "Jasper", "demotte": "Jasper", "remington": "Jasper",
    "wheatfield": "Jasper",
    # Newton County
    "kentland": "Newton", "morocco": "Newton", "goodland": "Newton",
    "brook": "Newton",
    # Benton County
    "fowler": "Benton", "oxford": "Benton",
    # Warren County (Indiana)
    "williamsport": "Warren",
    # Fountain County
    "covington": "Fountain", "veedersburg": "Fountain", "attica": "Fountain",
    "kingman": "Fountain", "hillsboro": "Fountain",
    # Vermillion County (Indiana)
    "clinton": "Vermillion", "cayuga": "Vermillion", "newport": "Vermillion",
    "perrysville": "Vermillion",
    # Parke County
    "rockville": "Parke", "rosedale": "Parke", "marshall": "Parke",
    # Putnam County (Indiana)
    "greencastle": "Putnam", "cloverdale": "Putnam", "bainbridge": "Putnam",
    "fillmore": "Putnam", "roachdale": "Putnam", "russellville": "Putnam",
    # Montgomery County (Indiana)
    "crawfordsville": "Montgomery", "darlington": "Montgomery",
    "ladoga": "Montgomery", "linden": "Montgomery", "new market": "Montgomery",
    "waveland": "Montgomery", "waynetown": "Montgomery", "wingate": "Montgomery",
    # Sullivan County (Indiana)
    "sullivan": "Sullivan", "shelburn": "Sullivan", "hymera": "Sullivan",
    "carlisle": "Sullivan", "merom": "Sullivan",
    # Clay County (Indiana)
    "brazil": "Clay", "knightsville": "Clay", "harmony": "Clay",
    "carbon": "Clay",
    # Perry County (Indiana)
    "tell city": "Perry", "cannelton": "Perry", "troy": "Perry",
    # Spencer County (Indiana)
    "rockport": "Spencer", "santa claus": "Spencer", "dale": "Spencer",
    "chrisney": "Spencer", "grandview": "Spencer", "richland": "Spencer",
    # Jennings County
    "north vernon": "Jennings", "vernon": "Jennings",
    # Jackson County (Indiana)
    "seymour": "Jackson", "brownstown": "Jackson", "crothersville": "Jackson",
    "medora": "Jackson",
}


# City centroids (lat, lon). Used as final fallback when ZIP-centroid lookup
# fails. Coverage: every city present in CITY_COUNTY for the top 30+ counties.
# Outlying small towns may be absent — those fall through to city_only without
# coordinates and stay hidden from the map.
CITY_CENTROIDS: dict[str, tuple[float, float]] = {
    # Marion
    "indianapolis": (39.7684, -86.1581), "lawrence": (39.8389, -86.0252),
    "speedway": (39.7950, -86.2470), "beech grove": (39.7172, -86.0890),
    "southport": (39.6645, -86.1100), "geist": (39.9143, -85.9489),
    "castleton": (39.9000, -86.0500), "broad ripple": (39.8714, -86.1421),
    "irvington": (39.7728, -86.0698),
    # Hamilton
    "carmel": (39.9784, -86.1180), "fishers": (39.9568, -86.0134),
    "noblesville": (40.0456, -86.0086), "westfield": (40.0428, -86.1275),
    "cicero": (40.1240, -86.0136), "atlanta": (40.2120, -86.0289),
    "sheridan": (40.1340, -86.2206), "arcadia": (40.1748, -86.0211),
    # Hendricks
    "avon": (39.7628, -86.3997), "brownsburg": (39.8434, -86.3947),
    "plainfield": (39.7042, -86.3994), "danville": (39.7606, -86.5269),
    "pittsboro": (39.8642, -86.4669), "north salem": (39.8617, -86.6464),
    "lizton": (39.8967, -86.5364),
    # Johnson
    "greenwood": (39.6137, -86.1067), "franklin": (39.4806, -86.0552),
    "bargersville": (39.5239, -86.1681), "whiteland": (39.5495, -86.0775),
    "new whiteland": (39.5564, -86.0908), "edinburgh": (39.3553, -85.9656),
    "trafalgar": (39.4123, -86.1539),
    # Boone
    "zionsville": (39.9509, -86.2625), "lebanon": (40.0481, -86.4694),
    "whitestown": (39.9989, -86.3486), "thorntown": (40.1301, -86.6094),
    "jamestown": (39.9342, -86.6336),
    # Lake
    "crown point": (41.4170, -87.3653), "munster": (41.5400, -87.5125),
    "schererville": (41.4793, -87.4548), "highland": (41.5533, -87.4525),
    "hammond": (41.5834, -87.5000), "gary": (41.5934, -87.3464),
    "merrillville": (41.4828, -87.3328), "cedar lake": (41.3614, -87.4364),
    "st. john": (41.4498, -87.4717), "saint john": (41.4498, -87.4717),
    "st john": (41.4498, -87.4717),
    "dyer": (41.4942, -87.5217), "griffith": (41.5283, -87.4239),
    "lowell": (41.2906, -87.4203), "whiting": (41.6800, -87.4942),
    "east chicago": (41.6394, -87.4540), "winfield": (41.4422, -87.2867),
    "hobart": (41.5320, -87.2700), "lake station": (41.5750, -87.2390),
    # Porter
    "valparaiso": (41.4731, -87.0611), "portage": (41.5759, -87.1761),
    "chesterton": (41.6103, -87.0631), "porter": (41.6128, -87.0731),
    "burns harbor": (41.6383, -87.1331), "hebron": (41.3197, -87.2008),
    "kouts": (41.3128, -87.0258), "ogden dunes": (41.6225, -87.1881),
    "beverly shores": (41.6900, -86.9742),
    # Allen
    "fort wayne": (41.0793, -85.1394), "ft. wayne": (41.0793, -85.1394),
    "ft wayne": (41.0793, -85.1394),
    "new haven": (41.0708, -85.0125), "huntertown": (41.2261, -85.1722),
    "leo-cedarville": (41.2192, -85.0125), "leo": (41.2192, -85.0125),
    "grabill": (41.2117, -84.9572), "woodburn": (41.1281, -84.8517),
    "monroeville": (40.9742, -84.8689),
    # St. Joseph
    "south bend": (41.6764, -86.2520), "mishawaka": (41.6620, -86.1586),
    "granger": (41.7458, -86.1119), "osceola": (41.6628, -86.0808),
    "walkerton": (41.4661, -86.4836), "north liberty": (41.5331, -86.4264),
    "new carlisle": (41.7000, -86.5092), "lakeville": (41.5275, -86.2747),
    "notre dame": (41.7000, -86.2333),
    # Elkhart
    "elkhart": (41.6820, -85.9767), "goshen": (41.5820, -85.8344),
    "bristol": (41.7222, -85.8175), "middlebury": (41.6739, -85.7008),
    "nappanee": (41.4442, -85.9622), "wakarusa": (41.5375, -86.0228),
    "millersburg": (41.5283, -85.7000),
    # Tippecanoe
    "lafayette": (40.4167, -86.8753), "west lafayette": (40.4259, -86.9081),
    "battle ground": (40.5095, -86.8447), "dayton": (40.3784, -86.7717),
    "otterbein": (40.4914, -87.0967),
    # Madison (IN)
    "anderson": (40.1053, -85.6803), "pendleton": (40.0014, -85.7472),
    "elwood": (40.2773, -85.8364), "alexandria": (40.2628, -85.6717),
    "lapel": (40.0700, -85.8483), "ingalls": (39.9606, -85.7986),
    "frankton": (40.2217, -85.7775), "edgewood": (40.1067, -85.7508),
    # Vanderburgh
    "evansville": (37.9716, -87.5711), "darmstadt": (38.0934, -87.5808),
    # Monroe (IN)
    "bloomington": (39.1653, -86.5264), "ellettsville": (39.2353, -86.6253),
    "stinesville": (39.3025, -86.6453),
    # Clark (IN)
    "jeffersonville": (38.2776, -85.7372), "clarksville": (38.2967, -85.7594),
    "charlestown": (38.4515, -85.6669), "sellersburg": (38.3978, -85.7547),
    "henryville": (38.5400, -85.7681), "borden": (38.5667, -85.9494),
    "memphis": (38.4683, -85.7625),
    # Hancock
    "greenfield": (39.7869, -85.7694), "mccordsville": (39.9056, -85.9211),
    "fortville": (39.9325, -85.8489), "new palestine": (39.7531, -85.8853),
    "cumberland": (39.7836, -85.9536), "shirley": (39.8889, -85.5828),
    # Morgan (IN)
    "mooresville": (39.6131, -86.3744), "martinsville": (39.4276, -86.4275),
    "monrovia": (39.5781, -86.4844), "morgantown": (39.3739, -86.2628),
    "paragon": (39.3898, -86.5714),
    # Warrick
    "newburgh": (37.9442, -87.4053), "boonville": (38.0492, -87.2742),
    "chandler": (38.0436, -87.3683), "elberfeld": (38.1669, -87.4283),
    "lynnville": (38.2008, -87.3014),
    # Bartholomew
    "columbus": (39.2014, -85.9214), "hope": (39.3056, -85.7744),
    "elizabethtown": (39.1428, -85.8217),
    # Howard
    "kokomo": (40.4864, -86.1336), "russiaville": (40.4178, -86.2742),
    "greentown": (40.4783, -85.9636),
    # Vigo
    "terre haute": (39.4667, -87.4139), "west terre haute": (39.4670, -87.4750),
    "seelyville": (39.4936, -87.2606),
    # Delaware
    "muncie": (40.1934, -85.3864), "yorktown": (40.1736, -85.4894),
    "selma": (40.1733, -85.2658), "eaton": (40.3417, -85.3500),
    "albany": (40.3017, -85.2417), "daleville": (40.1206, -85.5556),
    "gaston": (40.3122, -85.5008),
    # LaPorte
    "la porte": (41.6106, -86.7228), "laporte": (41.6106, -86.7228),
    "michigan city": (41.7075, -86.8950), "long beach": (41.7459, -86.8800),
    "kingsbury": (41.5283, -86.6975), "westville": (41.5247, -86.8975),
    "rolling prairie": (41.6889, -86.6097), "trail creek": (41.7033, -86.8783),
    # Wayne (IN)
    "richmond": (39.8289, -84.8902), "centerville": (39.8186, -85.0083),
    "cambridge city": (39.8128, -85.1714), "hagerstown": (39.9111, -85.1614),
    "fountain city": (39.9572, -84.9181), "milton": (39.7858, -85.1497),
    "boston": (39.7531, -84.8525),
    # Kosciusko
    "warsaw": (41.2381, -85.8531), "winona lake": (41.2261, -85.8225),
    "syracuse": (41.4419, -85.7508), "milford": (41.4078, -85.8467),
    "mentone": (41.1772, -86.0353), "north webster": (41.3253, -85.6986),
    "pierceton": (41.2097, -85.7028), "leesburg": (41.3239, -85.8358),
    "silver lake": (41.0775, -85.8869),
    # Floyd
    "new albany": (38.2856, -85.8241), "georgetown": (38.2989, -85.9683),
    "greenville": (38.3675, -85.9889),
    # Grant
    "marion": (40.5583, -85.6594), "gas city": (40.4869, -85.6125),
    "upland": (40.4717, -85.4944), "fairmount": (40.4172, -85.6494),
    "jonesboro": (40.4750, -85.6308), "swayzee": (40.5119, -85.8261),
    "van buren": (40.6172, -85.5092),
    # DuBois
    "jasper": (38.3914, -86.9311), "huntingburg": (38.3000, -86.9533),
    "ferdinand": (38.2228, -86.8631), "birdseye": (38.3236, -86.6928),
    "holland": (38.2542, -87.0492), "ireland": (38.4144, -86.9711),
    # Shelby (IN)
    "shelbyville": (39.5217, -85.7769), "morristown": (39.6722, -85.6986),
    "fairland": (39.5900, -85.8689), "st. paul": (39.4292, -85.6306),
    "saint paul": (39.4292, -85.6306),
    # Cass (IN)
    "logansport": (40.7544, -86.3567), "galveston": (40.5806, -86.1875),
    "royal center": (40.8617, -86.5006), "walton": (40.6739, -86.2433),
    # Henry (IN)
    "new castle": (39.9289, -85.3700), "knightstown": (39.7958, -85.5267),
    "middletown": (40.0533, -85.5392), "lewisville": (39.8217, -85.3522),
    "spiceland": (39.8378, -85.4344),
    # Steuben
    "angola": (41.6353, -85.0036), "fremont": (41.7297, -84.9319),
    "ashley": (41.5292, -85.0708), "hudson": (41.5400, -85.0708),
    "orland": (41.7375, -85.1736),
    # Whitley
    "columbia city": (41.1597, -85.4886), "churubusco": (41.2331, -85.3225),
    "south whitley": (41.0822, -85.6258), "larwill": (41.1786, -85.6306),
    # Noble
    "kendallville": (41.4414, -85.2647), "ligonier": (41.4661, -85.5872),
    "albion": (41.4019, -85.4250), "rome city": (41.4978, -85.5103),
    "wolcottville": (41.5267, -85.3628), "avilla": (41.3633, -85.2386),
    # DeKalb (IN)
    "auburn": (41.3672, -85.0586), "garrett": (41.3486, -85.1336),
    "butler": (41.4253, -84.8717), "waterloo": (41.4267, -85.0228),
    # Wabash
    "wabash": (40.7978, -85.8203), "north manchester": (41.0008, -85.7681),
    "roann": (40.9136, -85.9236), "lagro": (40.8392, -85.7256),
    # Marshall
    "plymouth": (41.3434, -86.3097), "bremen": (41.4458, -86.1481),
    "argos": (41.2389, -86.2444), "bourbon": (41.2950, -86.1119),
    "culver": (41.2200, -86.4239), "lapaz": (41.4583, -86.3081),
    "la paz": (41.4583, -86.3081),
    # Fulton (IN)
    "rochester": (41.0639, -86.2153), "akron": (41.0397, -86.0275),
    "fulton": (40.9461, -86.2664), "kewanna": (41.0192, -86.4225),
    # Knox (IN)
    "vincennes": (38.6772, -87.5286), "bicknell": (38.7747, -87.3092),
    "monroe city": (38.6189, -87.3692), "oaktown": (38.8689, -87.4453),
    # Lawrence (IN)
    "bedford": (38.8611, -86.4872), "mitchell": (38.7322, -86.4736),
    "oolitic": (38.9078, -86.5269), "springville": (38.9408, -86.5708),
    # Greene (IN)
    "linton": (39.0345, -87.1656), "bloomfield": (39.0264, -86.9381),
    "jasonville": (39.1656, -87.2031), "lyons": (38.9881, -87.0828),
    "worthington": (39.1242, -86.9783),
    # Brown (IN)
    "nashville": (39.2056, -86.2511),
    # Owen
    "spencer": (39.2862, -86.7625), "gosport": (39.3525, -86.6678),
    # Posey
    "mount vernon": (37.9320, -87.8945), "poseyville": (38.1717, -87.7864),
    "new harmony": (38.1303, -87.9356), "cynthiana": (38.1872, -87.7022),
    # Gibson
    "princeton": (38.3553, -87.5675), "fort branch": (38.2522, -87.5806),
    "oakland city": (38.3403, -87.3445), "haubstadt": (38.2050, -87.5731),
    "owensville": (38.2728, -87.6925), "patoka": (38.4031, -87.5897),
    # Pike (IN)
    "petersburg": (38.4922, -87.2786), "winslow": (38.3839, -87.2178),
    # Daviess (IN)
    "washington": (38.6592, -87.1728), "elnora": (38.8775, -87.0903),
    "odon": (38.8425, -86.9928), "montgomery": (38.6661, -87.0394),
    # Martin
    "loogootee": (38.6783, -86.9133), "shoals": (38.6661, -86.7917),
    "crane": (38.8908, -86.9028),
    # Orange (IN)
    "paoli": (38.5572, -86.4683), "french lick": (38.5489, -86.6200),
    "west baden springs": (38.5664, -86.6147), "orleans": (38.6603, -86.4525),
    # Crawford (IN)
    "english": (38.3331, -86.4622), "marengo": (38.3683, -86.3464),
    "milltown": (38.3692, -86.2906),
    # Harrison (IN)
    "corydon": (38.2120, -86.1219), "lanesville": (38.2384, -85.9714),
    "elizabeth": (38.1217, -85.9858), "palmyra": (38.4061, -86.1083),
    # Washington (IN)
    "salem": (38.6056, -86.1011), "campbellsburg": (38.6517, -86.2647),
    "hardinsburg": (38.7669, -86.2750), "pekin": (38.5039, -86.0086),
    # Scott (IN)
    "scottsburg": (38.6856, -85.7700), "austin": (38.7572, -85.8083),
    # Jefferson (IN)
    "madison": (38.7359, -85.3800), "hanover": (38.7156, -85.4731),
    # Switzerland
    "vevay": (38.7475, -85.0689), "patriot": (38.8408, -84.8278),
    # Ohio (IN)
    "rising sun": (38.9489, -84.8542),
    # Dearborn
    "lawrenceburg": (39.0908, -84.8497), "aurora": (39.0578, -84.9036),
    "greendale": (39.0964, -84.8628), "dillsboro": (39.0186, -85.0606),
    # Ripley
    "versailles": (39.0697, -85.2517), "batesville": (39.3000, -85.2233),
    "milan": (39.1208, -85.1314), "osgood": (39.1283, -85.2920),
    "sunman": (39.2356, -85.1006),
    # Decatur (IN)
    "greensburg": (39.3373, -85.4836),
    # Franklin (IN)
    "brookville": (39.4192, -85.0125), "laurel": (39.5083, -85.1872),
    "metamora": (39.4453, -85.1431),
    # Union (IN)
    "liberty": (39.6361, -84.9303),
    # Fayette (IN)
    "connersville": (39.6411, -85.1411),
    # Rush
    "rushville": (39.6094, -85.4475), "milroy": (39.5125, -85.4783),
    # Randolph (IN)
    "winchester": (40.1714, -84.9811), "union city": (40.2003, -84.8050),
    "lynn": (40.0500, -84.9381), "farmland": (40.1869, -85.1267),
    # Jay
    "portland": (40.4344, -84.9778), "dunkirk": (40.3756, -85.2103),
    "redkey": (40.3506, -85.1481), "pennville": (40.4944, -85.1486),
    # Adams (IN)
    "decatur": (40.8303, -84.9292), "geneva": (40.5917, -84.9583),
    "berne": (40.6586, -84.9522),
    # Wells
    "bluffton": (40.7386, -85.1719), "ossian": (40.8795, -85.1672),
    "uniondale": (40.8261, -85.2503), "markle": (40.8253, -85.3403),
    # Huntington
    "huntington": (40.8831, -85.4972), "andrews": (40.8628, -85.6011),
    "roanoke": (40.9636, -85.3742), "warren": (40.6817, -85.4250),
    # Blackford
    "hartford city": (40.4506, -85.3700), "montpelier": (40.5544, -85.2792),
    # Miami (IN)
    "peru": (40.7536, -86.0686), "denver": (40.8689, -86.0764),
    "macy": (40.9594, -86.1356), "amboy": (40.6042, -85.9239),
    # Tipton
    "tipton": (40.2839, -86.0414), "windfall": (40.3625, -85.9583),
    "sharpsville": (40.3753, -86.0822),
    # Clinton (IN)
    "frankfort": (40.2792, -86.5108), "kirklin": (40.1981, -86.3633),
    "rossville": (40.4203, -86.5961), "michigantown": (40.3247, -86.3997),
    "mulberry": (40.3417, -86.6700),
    # Carroll (IN)
    "delphi": (40.5856, -86.6753), "flora": (40.5478, -86.5247),
    "camden": (40.6322, -86.5400),
    # White (IN)
    "monticello": (40.7458, -86.7647), "monon": (40.8678, -86.8783),
    "wolcott": (40.7589, -87.0431), "brookston": (40.5972, -86.8675),
    "reynolds": (40.7497, -86.8728), "chalmers": (40.6644, -86.8631),
    # Pulaski (IN)
    "winamac": (41.0517, -86.6047), "francesville": (40.9842, -86.8814),
    "medaryville": (41.0853, -86.8869),
    # Starke
    "knox": (41.2956, -86.6253), "north judson": (41.2156, -86.7758),
    "hamlet": (41.2592, -86.5847),
    # Jasper (IN)
    "rensselaer": (40.9367, -87.1525), "demotte": (41.1953, -87.1986),
    "remington": (40.7611, -87.1503), "wheatfield": (41.1272, -87.0594),
    # Newton
    "kentland": (40.7686, -87.4453), "morocco": (40.9469, -87.4528),
    "goodland": (40.7611, -87.2942), "brook": (40.8650, -87.3650),
    # Benton
    "fowler": (40.6181, -87.3219), "oxford": (40.5208, -87.2467),
    # Warren (IN)
    "williamsport": (40.2906, -87.2942),
    # Fountain
    "covington": (40.1414, -87.3956), "veedersburg": (40.1131, -87.2614),
    "attica": (40.2939, -87.2483), "kingman": (39.9978, -87.3083),
    "hillsboro": (40.1308, -87.1500),
    # Vermillion (IN)
    "clinton": (39.6597, -87.4017), "cayuga": (39.9494, -87.4622),
    "newport": (39.8839, -87.4081), "perrysville": (40.0822, -87.4358),
    # Parke
    "rockville": (39.7611, -87.2306), "rosedale": (39.6253, -87.2842),
    "marshall": (39.8500, -87.1875),
    # Putnam (IN)
    "greencastle": (39.6447, -86.8650), "cloverdale": (39.5208, -86.7917),
    "bainbridge": (39.7642, -86.8089), "fillmore": (39.6772, -86.7472),
    "roachdale": (39.8467, -86.8000),
    # Montgomery (IN)
    "crawfordsville": (40.0411, -86.9744), "darlington": (40.1100, -86.7747),
    "ladoga": (39.9181, -86.8000), "linden": (40.1872, -86.9078),
    "waveland": (39.8772, -87.0489), "waynetown": (40.0769, -87.0892),
    # Sullivan (IN)
    "sullivan": (39.0939, -87.4061), "shelburn": (39.1789, -87.3950),
    "hymera": (39.1825, -87.3000), "carlisle": (38.9622, -87.3953),
    # Clay (IN)
    "brazil": (39.5236, -87.1253), "knightsville": (39.5325, -87.0958),
    "harmony": (39.5400, -87.0742),
    # Perry (IN)
    "tell city": (37.9514, -86.7681), "cannelton": (37.9111, -86.7456),
    "troy": (37.9956, -86.8014),
    # Spencer (IN)
    "rockport": (37.8814, -87.0497), "santa claus": (38.1206, -86.9067),
    "dale": (38.1689, -86.9933), "chrisney": (38.0058, -87.0089),
    "grandview": (37.9264, -86.9758),
    # Jennings
    "north vernon": (39.0064, -85.6233), "vernon": (38.9919, -85.6094),
    # Jackson (IN)
    "seymour": (38.9586, -85.8903), "brownstown": (38.8786, -86.0414),
    "crothersville": (38.8011, -85.8392), "medora": (38.8264, -86.1697),
}
