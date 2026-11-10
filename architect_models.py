"""Files shared by the warehouse with approved architects."""
import base64
import hashlib
import re
import unicodedata
from pathlib import PurePath

BUCKET = 'architect-models-3d'
MAX_BYTES = 50 * 1024 * 1024
EXTENSIONS = {'.3dm', '.3ds', '.3mf', '.blend', '.dae', '.dwg', '.dxf', '.fbx',
              '.glb', '.gltf', '.iges', '.igs', '.max', '.obj', '.rfa', '.skp',
              '.step', '.stl', '.stp', '.zip'}
TEXTURE_EXTENSIONS = {'.bmp', '.exr', '.hdr', '.jpeg', '.jpg', '.png', '.tif', '.tiff', '.webp', '.zip'}
CATEGORIES = {'models': 'Modele 3D', 'textures': 'Tekstury'}
KEY = re.compile(r'^(?P<texture>texture-)?[0-9a-f]{64}\.(?P<filename>[A-Za-z0-9_-]{1,340})$')


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
