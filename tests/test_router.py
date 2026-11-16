from bot.features import router


def test_bare_nugget_is_a_fact():
    handler, rest = router.resolve("")
    assert handler is router.cmd_nugget
    assert rest == ""


def test_subcommand_routing():
    handler, rest = router.resolve("roll 2d6+3")
    assert handler is router.cmd_roll
    assert rest == "2d6+3"


def test_aliases():
    assert router.resolve("summary")[0] is router.cmd_summarize
    assert router.resolve("remind 10m hi")[0] is router.cmd_remind
    assert router.resolve("pings join")[0] is router.cmd_pinggroup
    assert router.resolve("incognito")[0] is router.cmd_secret


def test_dm_chat_subcommands():
    assert router.resolve("chats")[0] is router.cmd_chats
    assert router.resolve("chat new rust")[0] is router.cmd_chat
    assert router.resolve("chat new rust")[1] == "new rust"
    assert router.resolve("newchat rust")[0] is router.cmd_newchat
    assert router.resolve("secret")[0] is router.cmd_secret
    assert router.resolve("end --burn")[0] is router.cmd_end


def test_unknown_subcommand():
    handler, rest = router.resolve("nonsense")
    assert handler is None
    assert rest == "nonsense"


def test_every_subcommand_is_callable():
    assert router.SUBCOMMANDS
    for name, handler in router.SUBCOMMANDS.items():
        assert name == name.lower()
        assert callable(handler)
