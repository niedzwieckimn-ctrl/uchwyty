"""Administrator corrections; never create an identity or reset entitlements."""
import re

FIELDS = {'first_name':100, 'last_name':100, 'studio_name':200, 'email':254,
          'phone':60, 'instagram':250, 'website':500, 'shipping_address':1000, 'nip':30}


def profile_values(profile):
    return {key: profile.get(key) or '' for key in FIELDS}


def validate_profile(form):
    values = {key: str(form.get(key) or '').strip() for key in FIELDS}
    values['email'] = values['email'].lower()
    for key, limit in FIELDS.items():
        if len(values[key]) > limit or any(ord(c) < 32 and c not in '\n\r' for c in values[key]):
            raise ValueError('Sprawdź długość i treść wpisanych danych.')
    if any(not values[k] for k in ('first_name','last_name','studio_name','email','phone','instagram','shipping_address')):
        raise ValueError('Uzupełnij imię, nazwisko, pracownię, e-mail, telefon i adres wysyłki.')
    if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', values['email']):
        raise ValueError('Podaj poprawny adres e-mail.')
    if values['instagram'] and len(values['instagram']) < 2:
        raise ValueError('Podaj poprawną nazwę lub adres profilu Instagram.')
    if values['website'] and not values['website'].startswith(('https://','http://')):
        raise ValueError('Adres strony internetowej powinien zaczynać się od https:// lub http://.')
    return values


def save_profile(remote, architect_id, values, expected, actor):
    """Preflight before Auth writes. Reconcile uncertain Auth results before proceeding.

    Profile + audit commit together. A failure after an Auth correction is recoverable
    by the same form, without a reverse email change or a second identity.
    """
    args = dict(p_architect_id=str(architect_id), p_data=values, p_expected=expected,
                p_actor=actor, p_check_only=True)
    remote('/rest/v1/rpc/architect_admin_edit_profile',method='POST',payload=dict(args),timeout=20)
    if values['email'] != expected['email'].strip().lower():
        path = '/auth/v1/admin/users/' + str(architect_id)
        user = remote(path,timeout=15)
        if not isinstance(user,dict) or str(user.get('id')) != str(architect_id):
            raise ValueError('Nie udało się sprawdzić konta do logowania. Dane nie zostały zapisane.')
        current = str(user.get('email') or '').lower().strip()
        if current not in {expected['email'].lower().strip(),values['email']}:
            raise ValueError('Adres konta zmienił się w międzyczasie. Odśwież kartę i sprawdź dane.')
        if current != values['email']:
            try:
                # Preserve existing confirmation policy. No password, role or verification override.
                remote(path,method='PUT',payload={'email':values['email']},timeout=20)
            except Exception:
                user = remote(path,timeout=15)
                if not isinstance(user,dict) or str(user.get('email','')).lower().strip() != values['email']:
                    raise ValueError('Nie potwierdzono zmiany adresu do logowania. Sprawdź, czy adres nie jest zajęty, i odśwież kartę.') from None
    args['p_check_only'] = False
    try:
        remote('/rest/v1/rpc/architect_admin_edit_profile',method='POST',payload=args,timeout=20)
    except Exception:
        rows = remote('/rest/v1/architect_profiles',params={'select':','.join(FIELDS),
                      'id':'eq.'+str(architect_id),'limit':1},timeout=15)
        if not rows or profile_values(rows[0]) != values:
            raise ValueError('Nie potwierdzono zapisu wszystkich danych. Adres do logowania mógł już zostać poprawiony. Sprawdź dane i zapisz formularz ponownie.') from None
