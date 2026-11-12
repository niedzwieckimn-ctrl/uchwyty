"""Files shared by the warehouse with approved architects."""
import base64
import hashlib
import json
import re
import unicodedata
from pathlib import PurePath

BUCKET = 'architect-models-3d'
RELEASE = 'modele-20261112'
MAX_BYTES = 50 * 1024 * 1024
MAX_PROGRAM_BYTES = 2 * 1024 * 1024 * 1024
EXTENSIONS = {'.3dm', '.3ds', '.3mf', '.blend', '.dae', '.dwg', '.dxf', '.fbx',
              '.glb', '.gltf', '.iges', '.igs', '.max', '.obj', '.rfa', '.skp',
              '.step', '.stl', '.stp', '.zip'}
TEXTURE_EXTENSIONS = {'.bmp', '.exr', '.hdr', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp', '.zip'}
CATEGORIES = {'models': 'Modele 3D', 'textures': 'Tekstury'}
KEY = re.compile(r'^(?P<texture>texture-)?[0-9a-f]{64}\.(?P<filename>[A-Za-z0-9_-]{1,340})$')
PROGRAM_PACKAGES = {
    'DAE': ('SketchUp, Lumion', 'Niedzwieccy_COLLADA_1-4-1_UV.zip'),
    'OBJ': ('CAD Decor, 3ds Max i inne programy OBJ', 'Niedzwieccy_OBJ_MTL_mm.zip'),
    'RHINO': ('Rhino', 'Niedzwieccy_Rhino_3DM_v7_mm.zip'),
    'BIM': ('Archicad, Revit', 'Niedzwieccy_3DM_v5_siatki_BIM.zip'),
    'GLB': ('Blender, Twinmotion, D5 Render i inne programy GLB', 'Niedzwieccy_GLB_PBR.zip'),
    'DXF': ('AutoCAD, BricsCAD', 'Niedzwieccy_DXF_2010_siatki_kolor.zip'),
    'STL': ('FreeCAD, Autodesk Fusion', 'Niedzwieccy_STL_geometria_mm.zip'),
}


def program_key(code, filename, size):
    package = PROGRAM_PACKAGES.get(code)
    if not package or filename != package[1]:
        raise ValueError('Nazwa archiwum nie odpowiada wybranemu formatowi.')
    if not isinstance(size, int) or not 0 < size <= MAX_PROGRAM_BYTES:
        raise ValueError('Archiwum jest puste albo przekracza limit 2 GB.')
    return RELEASE + '/' + filename


def valid_key(key):
    filename_for(key)
    return key


def upload_key(filename, stream, category='models'):
    if category not in CATEGORIES:
        raise ValueError('Wybierz kategorię pliku.')
    extensions = TEXTURE_EXTENSIONS if category == 'textures' else EXTENSIONS
    name = unicodedata.normalize('NFC', (filename or '').replace('\\', '/').split('/')[-1]).strip()
    if (not name or len(name.encode('utf8')) > 255 or any(not c.isprintable() for c in name)
            or PurePath(name).suffix.lower() not in extensions):
        raise ValueError('Wybierz plik w formacie obsługiwanym przez wybraną kategorię lub archiwum ZIP.')
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_BYTES:
            raise ValueError('Plik przekracza limit 50 MB.')
        digest.update(chunk)
    if not size:
        raise ValueError('Plik jest pusty.')
    stream.seek(0)
    encoded = base64.urlsafe_b64encode(name.encode('utf8')).decode('ascii').rstrip('=')
    prefix = 'texture-' if category == 'textures' else ''
    return prefix + digest.hexdigest() + '.' + encoded


def filename_for(key):
    match = KEY.fullmatch(key)
    if not match:
        raise ValueError('Niepoprawny identyfikator pliku.')
    encoded = match['filename']
    return base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode('utf8')


def list_files(remote):
    files = []
    offset = 0
    while True:
        page = remote('/storage/v1/object/list/' + BUCKET, method='POST', payload={
            'prefix': '', 'limit': 100, 'offset': offset,
            'sortBy': {'column': 'name', 'order': 'asc'}})
        for item in page:
            key = item['name']
            if item.get('id') and KEY.fullmatch(key):
                files.append({'key': key, 'name': filename_for(key),
                              'category': 'textures' if key.startswith('texture-') else 'models',
                              'size': (item.get('metadata') or {}).get('size', 0)})
        if len(page) < 100:
            return sorted(files, key=lambda f: f['name'].casefold())
        offset += len(page)


def list_release_objects(remote):
    """List only this release; names returned by Storage are relative to its prefix."""
    objects = {}
    offset = 0
    while True:
        page = remote('/storage/v1/object/list/' + BUCKET, method='POST', payload={
            'prefix': RELEASE, 'limit': 100, 'offset': offset,
            'sortBy': {'column': 'name', 'order': 'asc'}})
        for item in page:
            if item.get('id'):
                objects[item['name']] = item
        if len(page) < 100:
            return objects
        offset += len(page)


def validate_release_index(raw, objects):
    if not raw or len(raw) > 2 * 1024 * 1024:
        raise ValueError('Indeks jest pusty albo przekracza 2 MB.')
    try:
        data = json.loads(raw)
    except (UnicodeError, ValueError):
        raise ValueError('Indeks nie jest poprawnym plikiem JSON.') from None
    collections = data.get('collections') if isinstance(data, dict) else None
    products = data.get('products') if isinstance(data, dict) else None
    if (not isinstance(data, dict) or data.get('version') != 2
            or not isinstance(collections, list) or len(collections) != 21
            or not isinstance(products, dict) or not products):
        raise ValueError('Indeks nie ma oczekiwanej struktury wydania.')
    package_names = {name for _, name in PROGRAM_PACKAGES.values()}
    collection_names = {c.get('file') for c in collections if isinstance(c, dict)}
    if collection_names != package_names:
        raise ValueError('Indeks wskazuje inną listę paczek programowych.')
    if not all(isinstance(name, str) for name in products.values()):
        raise ValueError('Indeks zawiera niepoprawną nazwę pliku.')
    filenames = collection_names | set(products.values())
    if not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_-]+\.zip', name) for name in filenames):
        raise ValueError('Indeks zawiera niepoprawną nazwę pliku.')
    missing = filenames - objects.keys()
    if missing:
        raise ValueError(f'Nie można opublikować indeksu: brakuje {len(missing)} paczek w magazynie.')
    for collection in collections:
        size = (objects[collection['file']].get('metadata') or {}).get('size')
        if int(size or 0) != collection.get('size'):
            raise ValueError('Rozmiar jednej z paczek programowych nie zgadza się z indeksem.')
    return data
