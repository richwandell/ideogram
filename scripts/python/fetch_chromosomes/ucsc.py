from concurrent.futures import ThreadPoolExecutor
from functools import partial

from .utils import *


eutils = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
esearch = eutils + 'esearch.fcgi?retmode=json'
esummary = eutils + 'esummary.fcgi?retmode=json'


def query_accession_from_eutils(assembly_uid):
    """Requests esummary from NCBI Assembly DB, returns assembly accession
    """
    global esummary

    asm_summary = esummary + '&db=assembly&id=' + assembly_uid

    # Example: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?retmode=json&db=assembly&id=255628
    data = json.loads(request(asm_summary))
    result = data['result'][assembly_uid]
    acc = result['assemblyaccession'] # Accession.version

    # Return GenBank accession if it's default, else find and return it
    if "GCA_" not in acc:
        acc = result['synonym']['genbank']

    return acc


def get_genbank_accession_from_ucsc_name(db, times, unfound_dbs, logger):
    """Queries NCBI EUtils for the GenBank accession of a UCSC asseembly name
    """
    global esearch
    acc = None
    t0 = time_ms()
    logger.info('Fetching GenBank accession from NCBI EUtils for: ' + db)

    asm_search = esearch + '&db=assembly&term=' + db

    # Example: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=assembly&retmode=json&term=panTro4
    data = json.loads(request(asm_search))
    id_list = data['esearchresult']['idlist']
    if len(id_list) > 0:
        assembly_uid = id_list[0]
        acc = query_accession_from_eutils(assembly_uid)
    else:
        unfound_dbs.append(db)
        
    times['ncbi'] += time_ms() - t0
    return [acc, times, unfound_dbs]


def query_ucsc_cytobandideo_db(cursor):
    # Excludes unplaced and unlocalized chromosomes
    query = ('''
        SELECT * FROM cytoBandIdeo
        WHERE chrom NOT LIKE "chrUn"
            AND chrom LIKE "chr%"
            AND chrom NOT LIKE "chr%\_%"
    ''')
    r = cursor.execute(query)
    if r <= 1:
        # Skip if result contains only e.g. chrMT
        return None
    return cursor


def get_bands_by_chr(cursor):
    bands_by_chr = {}

    cursor = query_ucsc_cytobandideo_db(cursor)
    if cursor is None:
        return None

    has_bands = False
    rows3 = cursor.fetchall()
    for row3 in rows3:
        chr, start, stop, band_name, stain = row3
        bands_by_chr = update_bands_by_chr(
            bands_by_chr, chr, band_name, start, stop, stain
        )
        if band_name != '':
            has_bands = True
    if has_bands is False:
        return None
    
    return bands_by_chr

def get_cytobandideo_table(cursor, db):
    """ Determine if cytobandIdeo table is present for this assembly DB
    """
    cytobandideo_table = None

    cursor.execute('USE ' + db)
    cursor.execute('SHOW TABLES; # for ' + db)
    rows2 = cursor.fetchall()
    for row2 in rows2:
        if row2[0] == 'cytoBandIdeo':
            cytobandideo_table = 1
            break
    
    return cytobandideo_table


def fetch_assembly_data(db_tuples_list, times, unfound_dbs, logger):
    """Queries UCSC DBs, called via a thread pool in fetch_ucsc_data
    """
    cursor = get_cursor('genome-mysql.soe.ucsc.edu', 'genome', db='UCSC',
        logger=logger)

    for db_tuple in db_tuples_list:
        db = db_tuple[0]

        cytobandideo_table = get_cytobandideo_table(cursor, db)
        if cytobandideo_table is None:
            continue

        bands_by_chr = get_bands_by_chr(cursor)
        if bands_by_chr is None:
            continue

        genbank_accession, times, unfound_dbs =\
            get_genbank_accession_from_ucsc_name(db, times, unfound_dbs, logger)

        asm_data = [db, genbank_accession, bands_by_chr]

        logger.info('Got UCSC data: ' + str(asm_data))
        return [asm_data, times, unfound_dbs]


def fetch_from_ucsc(logger, times, unfound_dbs):
    """Queries MySQL instances hosted by UCSC Genome Browser

    To connect via Terminal (e.g. to debug), run:
    mysql --user=genome --host=genome-mysql.soe.ucsc.edu -A
    """
    print('Entering fetch_from_ucsc, ucsc')
    t0 = time_ms()
    logger.info('Entering fetch_from_ucsc')
    connection = db_connect(
        host='genome-mysql.soe.ucsc.edu',
        user='genome'
    )
    logger.info('Connected to UCSC database')
    cursor = connection.cursor()

    db_map = {}
    org_map = {}

    cursor.execute('use hgcentral')
    cursor.execute('''
      SELECT name, scientificName FROM dbDb
        WHERE active = 1
    ''')
    rows = cursor.fetchall()

    for row in rows:
        db = row[0]
        # e.g. Homo sapiens -> homo-sapiens
        name_slug = row[1].lower().replace(' ', '-')
        db_map[db] = name_slug

    db_tuples = [item for item in db_map.items()]

    # Take the list of DBs we want to query for cytoBandIdeo data,
    # split it into 30 smaller lists,
    # then launch a new thread for each of those small new DB lists
    # to divide up the work of querying remote DBs.
    num_threads = 30
    db_tuples_lists = chunkify(db_tuples, num_threads)
    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        print('in ThreadPoolExecutor, ucsc')
        results = pool.map(
            partial(fetch_assembly_data, 
                logger=logger, times=times, unfound_dbs=unfound_dbs),
            db_tuples_lists
        )
        for result in results:
            if result is None:
                continue
            asm_data, times, unfound_dbs = result
            if name_slug in org_map:
                org_map[name_slug].append(asm_data)
            else:
                org_map[name_slug] = [asm_data]

    times['ucsc'] += time_ms() - t0
    return [org_map, times, unfound_dbs]