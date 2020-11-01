import re
def sanitize(input):
    """ Take a file content and sanitize it into a valid yaml """

    output = []

    for i in input.splitlines():
        if "error:" in i:
            i = i.replace("\\", "").replace('"', '')

            i = re.sub(r"(error\:)\s*(.*)", r'\1 "\2"', i)

        output.append(i)

    return "\n".join(output)
