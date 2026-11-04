class GitOpsPlugin:
    name = ""

    def desired_resources(self, tenant=None):
        raise NotImplementedError
