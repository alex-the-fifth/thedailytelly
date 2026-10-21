from test_platform import client,token_for


def test_requesting_a_fresh_confirmation_can_opt_out_before_registration(client):
    email='still-pending@example.com'
    assert client.post('/auth/email',json={'email':email,'accepted':True,'reminder':True}).status_code==202
    assert client.post('/auth/email',json={'email':email,'accepted':True,'reminder':False}).status_code==202
    assert client.post('/auth/verify',json={'token':token_for(email)}).status_code==200
    state=client.get('/account/setup').json()
    assert not state['reminder_enabled'] and state['reminder'] is None
