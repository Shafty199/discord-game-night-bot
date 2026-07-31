def get_game_name_from_url(url):

    try:

        game_name = (
            url
            .split("/app/")[1]
            .split("/")[1]
            .replace("_", " ")
        )

        return game_name


    except IndexError:

        return "Unknown Game"