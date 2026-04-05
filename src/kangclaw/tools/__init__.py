"""工具集注册。"""

from kangclaw.tools.file_tools import read_file, write_file, edit_file, list_files, grep_file
from kangclaw.tools.exec_tool import exec_command
from kangclaw.tools.web_tools import web_search, web_fetch
from kangclaw.tools.cron_tools import cron_list, cron_add, cron_remove
from kangclaw.tools.image_tools import (
    image_filter, image_watermark, image_convert,
)
from kangclaw.tools.send_tools import send_image

ALL_TOOLS = [
    read_file,
    write_file,
    edit_file,
    list_files,
    grep_file,
    exec_command,
    web_search,
    web_fetch,
    cron_list,
    cron_add,
    cron_remove,
    image_filter,
    image_watermark,
    image_convert,
    send_image,
]
