/* cttyhack —— 给一个 tty 上的程序一个【控制终端】。
 *
 * 【为什么需要它】
 *
 * VNC（tty0）上那个 shell 报了这么一句，而它是开机第一眼看到的东西：
 *
 *     /bin/sh: 0: can't access tty; job control turned off
 *
 * 意思是：这个 shell 没有控制终端，Ctrl-Z / fg / bg 都用不了。
 *
 * 根因是启动它的方式：
 *
 *     setsid sh -c '...' < /dev/tty0 > /dev/tty0 2>&1
 *
 * setsid 建了一个【新会话】，而新会话没有控制终端。之后即使把标准输入输出
 * 接到 /dev/tty0，那个终端也【不会】自动成为它的控制终端——Linux 的规矩是
 * 只有会话首进程【主动】调 ioctl(TIOCSCTTY) 才算数，而 shell 不做这件事
 * （那是 login/getty 的活儿）。
 *
 * 所以：写一个二十行的小程序，把这件事做掉。busybox 里那个叫 cttyhack，
 * 名字沿用。
 *
 * 【它做的事】
 *
 *   1. setsid()             —— 自己成为新会话的首进程（没有控制终端）
 *   2. open(tty)            —— 打开那个终端
 *   3. ioctl(TIOCSCTTY, 0)  —— ★ 关键的一下：把它认成自己的控制终端
 *   4. dup2 到 0/1/2
 *   5. exec 目标程序        —— 于是它一出生就有控制终端，不报那句，作业控制可用
 *
 * 【编译期依赖】
 *
 * 只是一个 C 文件，构建时用 gcc 编一次。**运行时不需要编译器。**
 */

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/ioctl.h>

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "用法: cttyhack <tty> <程序> [参数…]\n");
        return 2;
    }

    /* 1. 新会话。失败也无所谓——可能本来就是首进程。 */
    setsid();

    /* 2. 打开那个终端。 */
    int fd = open(argv[1], O_RDWR);
    if (fd < 0) {
        perror(argv[1]);
        return 1;
    }

    /* 3. ★ 认成控制终端。这一步是整件事的全部。 */
    ioctl(fd, TIOCSCTTY, 0);

    /* 4. 变成标准输入输出。 */
    dup2(fd, STDIN_FILENO);
    dup2(fd, STDOUT_FILENO);
    dup2(fd, STDERR_FILENO);
    if (fd > STDERR_FILENO)
        close(fd);

    /* 5. 换成目标程序。 */
    execvp(argv[2], &argv[2]);
    perror(argv[2]);
    return 1;
}
